"""
聊天服务
========

按 thread 加载 Conversation → 校验 user_id → Agent Factory →
ainvoke / astream / resume；历史消息直接读 checkpointer，不触发 Agent 编译。

长耗时路径（ainvoke / SSE）在组装完成后立刻关闭 DB Session，
避免流式输出期间占着连接池与未提交事务。

SSE 事件：meta / token / tool_start / tool_end / todo / ping / done / error。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import NotFoundError
from deepagents_app.config import Settings, get_settings
from deepagents_app.db.session import get_async_session_factory
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id
from deepagents_app.services.agent_factory import build_agent_from_methodology
from deepagents_app.services.conversation import get_conversation_by_thread
from deepagents_app.services.message_serde import (
    extract_final_text,
    msg_role as _msg_role,
    serialize_interrupts,
    serialize_messages,
    tool_calls_payload as _tool_calls_payload,
)
from deepagents_app.services.stream_limiter import (
    acquire_stream_slot,
    close_redis_stream_slots_client,
    release_stream_slot,
)
from deepagents_app.utils.text import normalize_message_content
from deepagents_app.workspace import user_workspace_dir, workspace_context

logger = logging.getLogger(__name__)

_SSE_PING_INTERVAL_SECONDS = 15.0


def validate_chat_message(message: str, settings: Settings | None = None) -> None:
    """校验聊天消息长度；超限抛 ``BusinessError``。"""
    cfg = settings or get_settings()
    text = message if message is not None else ""
    max_chars = int(cfg.chat_message_max_chars)
    if len(text) > max_chars:
        from deepagents_app.api.errors import BusinessError

        raise BusinessError(f"消息过长：最多 {max_chars} 字符")


@dataclass(frozen=True)
class PreparedChat:
    """组装完成后即可脱离 DB 的运行时句柄。"""

    user_id: str
    thread_id: str
    methodology_id: str
    methodology_version: int
    agent: Any
    settings: Settings
    config: dict[str, Any]


def _pack_result(
    *,
    thread_id: str,
    result: dict[str, Any],
    methodology_id: str,
    methodology_version: int,
) -> dict[str, Any]:
    """统一 chat / resume 响应结构；``__interrupt__`` 表示 HITL 暂停。"""
    interrupts = result.get("__interrupt__")
    return {
        "thread_id": thread_id,
        "reply": extract_final_text(result),
        "interrupted": bool(interrupts),
        "interrupt": serialize_interrupts(interrupts),
        "methodology_id": methodology_id,
        "methodology_version": methodology_version,
    }


def _runtime_config(user_id: str, thread_id: str) -> dict[str, Any]:
    """LangGraph 运行配置：thread_id 需带 user 前缀，避免跨用户 checkpoint 串话。"""
    return {
        "configurable": {
            "thread_id": checkpoint_thread_id(user_id, thread_id),
        }
    }


async def prepare_chat(
    *,
    user_id: str,
    thread_id: str,
    settings: Settings | None = None,
) -> PreparedChat:
    """
    短事务加载会话并组装 Agent，返回后 Session 已关闭。

    供 chat / SSE / resume 共用：LLM 执行阶段不再占用连接池。
    """
    settings = settings or get_settings()
    db = get_async_session_factory()()
    try:
        conversation = await get_conversation_by_thread(db, thread_id, user_id=user_id)
        if conversation is None:
            raise NotFoundError(f"会话不存在：thread_id={thread_id}")

        methodology_id = conversation.methodology_id
        methodology_version = int(conversation.methodology_version)
        agent = await build_agent_from_methodology(
            db,
            methodology_id,
            owner_user_id=user_id,
            version=methodology_version,
            settings=settings,
        )
        await db.commit()
        from deepagents_app.services.revisions import flush_cache_invalidations

        flush_cache_invalidations(db)
        return PreparedChat(
            user_id=user_id,
            thread_id=thread_id,
            methodology_id=methodology_id,
            methodology_version=methodology_version,
            agent=agent,
            settings=settings,
            config=_runtime_config(user_id, thread_id),
        )
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def _sse(event: str, data: dict[str, Any] | str) -> str:
    """格式化为 SSE 帧。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _todo_payload_from_msg(msg: Any) -> dict[str, Any] | None:
    """从消息内容里识别粗粒度 todo 进度（若模型/中间件写入结构化标记）。"""
    content = getattr(msg, "content", None)
    if not isinstance(content, list):
        return None
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in {"todo", "todos"} or "todos" in block:
            return {"todos": block.get("todos") or block.get("items") or block}
    return None


async def _aiter_stream_events(
    agent: Any, payload: Any, config: dict[str, Any]
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """
    流式事件：(event, data)。

    TypeError 仅在创建 astream 迭代器时兜底到 ainvoke，避免半途重跑重复计费。
    """
    try:
        stream = agent.astream(payload, config=config, stream_mode="messages")
    except TypeError:
        result = await agent.ainvoke(payload, config=config)
        text = extract_final_text(result if isinstance(result, dict) else {})
        if text:
            yield "token", {"text": text}
        return

    yielded_token = False
    try:
        async for item in stream:
            msg = item[0] if isinstance(item, tuple) else item
            role = _msg_role(msg)

            if isinstance(msg, AIMessageChunk) or type(msg).__name__ == "AIMessageChunk":
                piece = normalize_message_content(getattr(msg, "content", None))
                if piece:
                    yielded_token = True
                    yield "token", {"text": piece}
                tool_calls = _tool_calls_payload(msg)
                if tool_calls:
                    for tc in tool_calls:
                        yield "tool_start", {
                            "id": tc.get("id"),
                            "name": tc.get("name"),
                            "args": tc.get("args") or {},
                        }
                continue

            if role == "assistant":
                # 完整 AIMessage：推 tool_calls，不重复推全文
                tool_calls = _tool_calls_payload(msg)
                if tool_calls:
                    for tc in tool_calls:
                        yield "tool_start", {
                            "id": tc.get("id"),
                            "name": tc.get("name"),
                            "args": tc.get("args") or {},
                        }
                todo = _todo_payload_from_msg(msg)
                if todo:
                    yield "todo", todo
                name = getattr(msg, "name", None)
                if name and name not in {None, "", "model"}:
                    yield "subagent", {"name": str(name)}
                continue

            if role == "tool":
                yield "tool_end", {
                    "id": getattr(msg, "tool_call_id", None),
                    "name": getattr(msg, "name", None),
                    "content": normalize_message_content(getattr(msg, "content", None)),
                }
    except TypeError:
        # 半途 TypeError：不再 ainvoke，避免重复输出；留给上层读最终状态
        logger.warning("astream 中途 TypeError，停止增量推送并回退读最终状态")

    if not yielded_token:
        result = await _afinal_state_result(agent, config)
        text = extract_final_text(result)
        if text:
            yield "token", {"text": text}


async def _afinal_state_result(agent: Any, config: dict[str, Any]) -> dict[str, Any]:
    """读图状态，拼成与 ainvoke 相近的结果字典（含 interrupt）。"""
    result: dict[str, Any] = {"messages": []}
    try:
        state = await agent.aget_state(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取 chat 最终状态失败: %s", exc)
        return result

    values = getattr(state, "values", None) or {}
    if isinstance(values, dict):
        result["messages"] = values.get("messages") or []

    tasks = getattr(state, "tasks", None) or ()
    interrupts: list[Any] = []
    for task in tasks:
        interrupts.extend(getattr(task, "interrupts", None) or ())
    if interrupts:
        result["__interrupt__"] = interrupts
    return result


async def _aiter_sse(
    prepared: PreparedChat,
    payload: Any,
    *,
    meta: dict[str, Any],
    log_label: str,
) -> AsyncIterator[str]:
    """SSE 共用内核：meta → 事件* → done | error（执行期无 DB）；空闲发 ping。

    路由应先 ``prepare_chat``，再 ``acquire_stream_slot``，最后进入本函数，
    避免冷编译占用流式并发槽。
    """
    yield _sse("meta", meta)
    logger.info(
        "%s user=%s thread=%s methodology=%s v%s",
        log_label,
        prepared.user_id,
        prepared.thread_id,
        prepared.methodology_id,
        prepared.methodology_version,
    )
    assembled = ""
    next_task: asyncio.Task[tuple[str, dict[str, Any]]] | None = None
    event_iter: AsyncIterator[tuple[str, dict[str, Any]]] | None = None
    try:
        with workspace_context(
            user_workspace_dir(prepared.settings, prepared.user_id, ensure=False)
        ):
            event_iter = _aiter_stream_events(
                prepared.agent, payload, prepared.config
            ).__aiter__()
            while True:
                if next_task is None:
                    next_task = asyncio.create_task(event_iter.__anext__())
                done, _pending = await asyncio.wait(
                    {next_task}, timeout=_SSE_PING_INTERVAL_SECONDS
                )
                if not done:
                    # 超时不取消 next_task，心跳后继续等同一任务
                    yield _sse("ping", {"ok": True})
                    continue
                try:
                    event, data = next_task.result()
                except StopAsyncIteration:
                    break
                finally:
                    next_task = None
                if event == "token":
                    assembled += str(data.get("text") or "")
                yield _sse(event, data)
            result = await _afinal_state_result(prepared.agent, prepared.config)
        packed = _pack_result(
            thread_id=prepared.thread_id,
            result=result,
            methodology_id=prepared.methodology_id,
            methodology_version=prepared.methodology_version,
        )
        if not packed["reply"] and assembled:
            packed["reply"] = assembled
        yield _sse("done", packed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("%s failed thread=%s", log_label, prepared.thread_id)
        # 不对客户端暴露内部异常细节（路径 / SQL / SDK）
        yield _sse(
            "error",
            {
                "message": "对话执行失败，请稍后重试",
                "detail": type(exc).__name__,
            },
        )
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except (asyncio.CancelledError, StopAsyncIteration, Exception):  # noqa: BLE001
                pass
        if event_iter is not None:
            aclose = getattr(event_iter, "aclose", None)
            if callable(aclose):
                with suppress(asyncio.CancelledError, Exception):
                    await aclose()


async def chat(
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    """发送一轮用户消息：短事务组装后关闭 DB，再 ``ainvoke``。"""
    settings = get_settings()
    validate_chat_message(message, settings)
    prepared = await prepare_chat(user_id=user_id, thread_id=thread_id, settings=settings)
    logger.info(
        "chat user=%s thread=%s methodology=%s v%s",
        user_id,
        thread_id,
        prepared.methodology_id,
        prepared.methodology_version,
    )
    with workspace_context(
        user_workspace_dir(prepared.settings, user_id, ensure=False)
    ):
        result = await prepared.agent.ainvoke(
            {"messages": [{"role": "user", "content": message}]},
            config=prepared.config,
        )
    return _pack_result(
        thread_id=thread_id,
        result=result,
        methodology_id=prepared.methodology_id,
        methodology_version=prepared.methodology_version,
    )


async def iter_chat_sse(
    *,
    user_id: str,
    thread_id: str,
    message: str,
    prepared: PreparedChat | None = None,
) -> AsyncIterator[str]:
    """SSE：组装阶段独占短事务；流式阶段不再持有 Session。

    路由可先 ``prepare_chat`` 再抢槽，将结果作为 ``prepared`` 传入，避免重复组装。
    """
    settings = get_settings()
    validate_chat_message(message, settings)
    if prepared is None:
        prepared = await prepare_chat(
            user_id=user_id, thread_id=thread_id, settings=settings
        )
    async for chunk in _aiter_sse(
        prepared,
        {"messages": [{"role": "user", "content": message}]},
        meta={
            "thread_id": thread_id,
            "methodology_id": prepared.methodology_id,
            "methodology_version": prepared.methodology_version,
        },
        log_label="chat stream",
    ):
        yield chunk


async def resume_agent(
    agent: Any,
    config: dict[str, Any],
    *,
    approve: bool = True,
) -> dict[str, Any]:
    """对已编译 Agent 执行 HITL resume（``ainvoke`` + ``Command``）。"""
    return await agent.ainvoke(
        _resume_command(approve),
        config=config,
    )


def _resume_command(approve: bool) -> Any:
    """构造 LangGraph HITL resume 命令，避免同步与 SSE 路径的载荷漂移。"""
    from langgraph.types import Command

    decision_type = "approve" if approve else "reject"
    return Command(resume={"decisions": [{"type": decision_type}]})


async def resume_chat(
    *,
    user_id: str,
    thread_id: str,
    approve: bool = True,
) -> dict[str, Any]:
    """HITL 恢复：短事务组装后关闭 DB，再 resume。"""
    prepared = await prepare_chat(user_id=user_id, thread_id=thread_id)
    logger.info(
        "chat resume user=%s thread=%s decision=%s",
        user_id,
        thread_id,
        "approve" if approve else "reject",
    )
    with workspace_context(
        user_workspace_dir(prepared.settings, user_id, ensure=False)
    ):
        result = await resume_agent(
            prepared.agent, prepared.config, approve=approve
        )
    return _pack_result(
        thread_id=thread_id,
        result=result,
        methodology_id=prepared.methodology_id,
        methodology_version=prepared.methodology_version,
    )


async def iter_resume_sse(
    *,
    user_id: str,
    thread_id: str,
    approve: bool = True,
    prepared: PreparedChat | None = None,
) -> AsyncIterator[str]:
    """HITL 恢复的 SSE：组装后释放 DB，再 ``astream``。

    路由可先 ``prepare_chat`` 再抢槽，将结果作为 ``prepared`` 传入。
    """
    if prepared is None:
        prepared = await prepare_chat(user_id=user_id, thread_id=thread_id)
    decision_type = "approve" if approve else "reject"
    async for chunk in _aiter_sse(
        prepared,
        _resume_command(approve),
        meta={
            "thread_id": thread_id,
            "methodology_id": prepared.methodology_id,
            "methodology_version": prepared.methodology_version,
            "decision": decision_type,
        },
        log_label="chat resume stream",
    ):
        yield chunk


async def get_conversation_messages(
    db: AsyncSession,
    *,
    user_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """只读：直接从 checkpointer 取历史，不编译 Agent、不物化 Skills。"""
    conversation = await get_conversation_by_thread(db, thread_id, user_id=user_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")

    config = _runtime_config(user_id, thread_id)
    messages: list[Any] = []
    interrupted = False
    interrupt: list[dict[str, Any]] | None = None

    try:
        checkpointer = build_checkpointer(get_settings())
        cp_tuple = await checkpointer.aget_tuple(config)
        if cp_tuple is not None:
            checkpoint = getattr(cp_tuple, "checkpoint", None) or {}
            values = checkpoint.get("channel_values") or {}
            if isinstance(values, dict):
                messages = values.get("messages") or []
            for task in getattr(cp_tuple, "tasks", None) or ():
                interrupts = getattr(task, "interrupts", None) or ()
                if interrupts:
                    interrupted = True
                    interrupt = serialize_interrupts(interrupts)
                    break
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取会话状态失败 thread=%s: %s", thread_id, exc)

    return {
        "thread_id": thread_id,
        "methodology_id": conversation.methodology_id,
        "methodology_version": conversation.methodology_version,
        "messages": serialize_messages(messages),
        "interrupted": interrupted,
        "interrupt": interrupt,
    }
