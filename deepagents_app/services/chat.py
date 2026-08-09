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
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessageChunk
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import CapacityError, NotFoundError
from deepagents_app.config import Settings, get_settings
from deepagents_app.db.session import get_async_session_factory
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id
from deepagents_app.services.agent_factory import build_agent_from_methodology
from deepagents_app.services.conversation import get_conversation_by_thread
from deepagents_app.utils.text import normalize_message_content
from deepagents_app.workspace import user_workspace_dir, workspace_context

logger = logging.getLogger(__name__)

_SSE_PING_INTERVAL_SECONDS = 15.0
_stream_semaphore: asyncio.Semaphore | None = None
_stream_semaphore_limit: int | None = None
_REDIS_STREAM_KEY = "deepagents:chat_stream_inflight"
# 原子抢槽：超限则立即 DECR，避免 INCR/判断/DECR 竞态超卖
_REDIS_ACQUIRE_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if n <= tonumber(ARGV[1]) then
  return n
end
redis.call('DECR', KEYS[1])
return -1
"""
_redis_slots_client: Any | None = None
_redis_slots_lock: asyncio.Lock | None = None


class StreamSlot:
    """流式槽位句柄；结束时必须 ``await release()``。"""

    async def release(self) -> None:  # noqa: B027
        return


class _LocalStreamSlot(StreamSlot):
    def __init__(self, gate: asyncio.Semaphore) -> None:
        self._gate = gate

    async def release(self) -> None:
        self._gate.release()


class _RedisStreamSlot(StreamSlot):
    def __init__(self, client: Any) -> None:
        self._client = client
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            n = await self._client.decr(_REDIS_STREAM_KEY)
            if int(n) < 0:
                await self._client.set(_REDIS_STREAM_KEY, 0)
        except Exception:  # noqa: BLE001
            logger.debug("释放 Redis 流式槽位失败", exc_info=True)


def _stream_gate(settings: Settings) -> asyncio.Semaphore | None:
    """按配置惰性创建进程内流式并发闸门；0 表示不限。"""
    global _stream_semaphore, _stream_semaphore_limit
    limit = int(getattr(settings, "chat_stream_max_concurrent", 0) or 0)
    if limit <= 0:
        return None
    if _stream_semaphore is None or _stream_semaphore_limit != limit:
        _stream_semaphore = asyncio.Semaphore(limit)
        _stream_semaphore_limit = limit
    return _stream_semaphore


def _use_redis_stream_limiter(settings: Settings) -> bool:
    mode = (settings.chat_stream_limiter or "auto").strip().lower()
    if mode == "redis":
        return True
    if mode == "local":
        return False
    return int(settings.api_workers) > 1


async def _get_redis_slots_client(settings: Settings) -> Any:
    global _redis_slots_client, _redis_slots_lock
    if _redis_slots_lock is None:
        _redis_slots_lock = asyncio.Lock()
    async with _redis_slots_lock:
        if _redis_slots_client is None:
            import redis.asyncio as aioredis

            _redis_slots_client = aioredis.from_url(
                settings.redis_url,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        return _redis_slots_client


async def close_redis_stream_slots_client() -> None:
    """lifespan 退出时关闭流式限流 Redis 客户端。"""
    global _redis_slots_client
    if _redis_slots_lock is None:
        client = _redis_slots_client
        _redis_slots_client = None
    else:
        async with _redis_slots_lock:
            client = _redis_slots_client
            _redis_slots_client = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 Redis 流式槽位客户端失败", exc_info=True)


async def _acquire_redis_stream_slot(settings: Settings, limit: int) -> StreamSlot:
    """跨 worker 用 Redis 原子计数器抢槽；超时 → CapacityError。"""
    client = await _get_redis_slots_client(settings)
    timeout = float(getattr(settings, "chat_stream_acquire_timeout_seconds", 1.0) or 0)
    deadline = asyncio.get_running_loop().time() + (0.001 if timeout <= 0 else timeout)
    while True:
        n = int(
            await client.eval(
                _REDIS_ACQUIRE_LUA,
                1,
                _REDIS_STREAM_KEY,
                limit,
                86_400,
            )
        )
        if n > 0:
            return _RedisStreamSlot(client)
        if asyncio.get_running_loop().time() >= deadline:
            raise CapacityError("流式对话繁忙，请稍后重试")
        await asyncio.sleep(0.05)


async def acquire_stream_slot(settings: Settings | None = None) -> StreamSlot | None:
    """
    在打开 SSE 之前抢占流式槽位。

    超时或立即不可得时抛 ``CapacityError``（映射 429），避免无界排队。
    返回的句柄须在流结束后 ``await release()``；无限制时返回 None。
    多 worker（或 ``CHAT_STREAM_LIMITER=redis``）时用 Redis 全局限流。
    """
    settings = settings or get_settings()
    limit = int(getattr(settings, "chat_stream_max_concurrent", 0) or 0)
    if limit <= 0:
        return None

    if _use_redis_stream_limiter(settings):
        try:
            return await _acquire_redis_stream_slot(settings, limit)
        except CapacityError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 流式限流不可用，回退进程内闸门: %s", exc)

    gate = _stream_gate(settings)
    if gate is None:
        return None
    timeout = float(getattr(settings, "chat_stream_acquire_timeout_seconds", 1.0) or 0)
    wait_s = 0.001 if timeout <= 0 else timeout
    try:
        await asyncio.wait_for(gate.acquire(), timeout=wait_s)
    except TimeoutError as exc:
        raise CapacityError("流式对话繁忙，请稍后重试") from exc
    return _LocalStreamSlot(gate)


async def release_stream_slot(slot: StreamSlot | None) -> None:
    if slot is not None:
        await slot.release()

def validate_chat_message(message: str, settings: Settings | None = None) -> None:
    """校验聊天消息长度；超限抛 ``BusinessError``。"""
    cfg = settings or get_settings()
    text = message if message is not None else ""
    max_chars = int(cfg.chat_message_max_chars)
    if len(text) > max_chars:
        from deepagents_app.api.errors import BusinessError

        raise BusinessError(f"消息过长：最多 {max_chars} 字符")


def _validate_chat_message(message: str, settings: Settings) -> None:
    validate_chat_message(message, settings)


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


def _msg_role(msg: Any) -> str:
    """LangChain Message.type / OpenAI role → 前端 role。"""
    raw = getattr(msg, "type", None) or (
        msg.get("role") if isinstance(msg, dict) else None
    )
    raw = str(raw or "unknown")
    mapping = {
        "human": "user",
        "ai": "assistant",
        "assistant": "assistant",
        "user": "user",
        "system": "system",
        "tool": "tool",
    }
    return mapping.get(raw, raw)


def _tool_calls_payload(msg: Any) -> list[dict[str, Any]] | None:
    raw = getattr(msg, "tool_calls", None)
    if raw is None and isinstance(msg, dict):
        raw = msg.get("tool_calls")
    if not raw:
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            out.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "args": item.get("args") or item.get("arguments") or {},
                }
            )
            continue
        out.append(
            {
                "id": getattr(item, "id", None),
                "name": getattr(item, "name", None),
                "args": getattr(item, "args", None) or {},
            }
        )
    return out or None


def serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """将 LangChain / dict 消息转为前端可用结构（保留 tool_calls）。"""
    out: list[dict[str, Any]] = []
    for msg in messages or []:
        role = _msg_role(msg)
        content = normalize_message_content(
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        name = getattr(msg, "name", None) or (
            msg.get("name") if isinstance(msg, dict) else None
        )
        tool_calls = _tool_calls_payload(msg)
        tool_call_id = getattr(msg, "tool_call_id", None) or (
            msg.get("tool_call_id") if isinstance(msg, dict) else None
        )
        if not content and role not in {"user", "assistant"} and not tool_calls:
            continue
        row: dict[str, Any] = {"role": role, "content": content, "name": name}
        if tool_calls:
            row["tool_calls"] = tool_calls
        if tool_call_id:
            row["tool_call_id"] = tool_call_id
        out.append(row)
    return out


def extract_final_text(result: dict[str, Any]) -> str:
    """从 agent.ainvoke / invoke 结果取出最后一条 AI 文本。"""
    messages = result.get("messages") or []
    for msg in reversed(messages):
        role = _msg_role(msg)
        content = normalize_message_content(
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        if role == "assistant" and content:
            return content
    return ""


def serialize_interrupts(interrupts: Any) -> list[dict[str, Any]] | None:
    """
    将 LangGraph ``__interrupt__`` 转为前端可解析结构。

    期望 HITL value 含 ``action_requests``（工具名 / 参数）；无法识别时降级为 raw。
    """
    if not interrupts:
        return None
    items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
    out: list[dict[str, Any]] = []
    for item in items:
        iid = getattr(item, "id", None)
        value = getattr(item, "value", item)
        actions: list[dict[str, Any]] = []
        raw_actions = None
        if isinstance(value, dict):
            raw_actions = value.get("action_requests")
        elif hasattr(value, "get"):
            try:
                raw_actions = value.get("action_requests")  # type: ignore[union-attr]
            except Exception:  # noqa: BLE001
                raw_actions = None
        if isinstance(raw_actions, list):
            for req in raw_actions:
                if isinstance(req, dict):
                    actions.append(
                        {
                            "name": req.get("name"),
                            "args": req.get("args") or {},
                            "description": req.get("description"),
                        }
                    )
                else:
                    actions.append(
                        {
                            "name": getattr(req, "name", None),
                            "args": getattr(req, "args", None) or {},
                            "description": getattr(req, "description", None),
                        }
                    )
        entry: dict[str, Any] = {"id": iid, "actions": actions}
        if not actions:
            try:
                entry["raw"] = value if isinstance(value, (dict, list, str, int, float, bool)) or value is None else str(value)
            except Exception:  # noqa: BLE001
                entry["raw"] = repr(value)
        out.append(entry)
    return out or None


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


async def chat(
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    """发送一轮用户消息：短事务组装后关闭 DB，再 ``ainvoke``。"""
    settings = get_settings()
    _validate_chat_message(message, settings)
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
    _validate_chat_message(message, settings)
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
    from langgraph.types import Command

    decision_type = "approve" if approve else "reject"
    return await agent.ainvoke(
        Command(resume={"decisions": [{"type": decision_type}]}),
        config=config,
    )


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
    from langgraph.types import Command

    if prepared is None:
        prepared = await prepare_chat(user_id=user_id, thread_id=thread_id)
    decision_type = "approve" if approve else "reject"
    async for chunk in _aiter_sse(
        prepared,
        Command(resume={"decisions": [{"type": decision_type}]}),
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
