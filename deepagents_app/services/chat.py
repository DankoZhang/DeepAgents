"""
聊天服务
========

按 thread 加载 Conversation → 校验 user_id → Agent Factory →
invoke / stream / resume；历史消息直接读 checkpointer，不触发 Agent 编译。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import NotFoundError
from deepagents_app.config import get_settings
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id
from deepagents_app.services.agent_factory import build_agent_from_methodology
from deepagents_app.services.conversation import get_conversation_by_thread
from deepagents_app.utils.text import normalize_message_content
from deepagents_app.workspace import user_workspace_dir, workspace_context

logger = logging.getLogger(__name__)


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


def serialize_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """将 LangChain / dict 消息转为前端可用结构。"""
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
        if not content and role not in {"user", "assistant"}:
            continue
        out.append({"role": role, "content": content, "name": name})
    return out


def extract_final_text(result: dict[str, Any]) -> str:
    """从 agent.invoke 结果取出最后一条 AI 文本。"""
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
        "interrupt": str(interrupts) if interrupts else None,
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


def _prepare_agent(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
):
    """加载会话并组装 Agent；返回 (conversation, agent, settings, config)。"""
    conversation = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")

    settings = get_settings()
    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        owner_user_id=user_id,
        version=conversation.methodology_version,
        settings=settings,
    )
    config = _runtime_config(user_id, thread_id)
    return conversation, agent, settings, config


def _sse(event: str, data: dict[str, Any] | str) -> str:
    """格式化为 SSE 帧。"""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _iter_message_tokens(agent: Any, payload: Any, config: dict[str, Any]) -> Iterator[str]:
    """
    从 LangGraph stream 抽出 assistant 文本增量。

    优先 ``stream_mode="messages"`` 的 Chunk；不支持或无增量时退化为 invoke 一次 yield。
    """
    stream_fn = getattr(agent, "stream", None)
    if not callable(stream_fn):
        result = agent.invoke(payload, config=config)
        text = extract_final_text(result if isinstance(result, dict) else {})
        if text:
            yield text
        return

    yielded = False
    try:
        for item in stream_fn(payload, config=config, stream_mode="messages"):
            msg = item[0] if isinstance(item, tuple) else item
            if _msg_role(msg) != "assistant":
                continue
            # 只推送流式 Chunk，避免完整 AIMessage 把全文再吐一遍
            if "Chunk" not in type(msg).__name__:
                continue
            piece = normalize_message_content(getattr(msg, "content", None))
            if piece:
                yielded = True
                yield piece
    except TypeError:
        result = agent.invoke(payload, config=config)
        text = extract_final_text(result if isinstance(result, dict) else {})
        if text:
            yield text
        return

    if not yielded:
        # 有的图不产出 message chunk：再 invoke 一轮拿最终回复
        # 注意：若 stream 已推进图状态，invoke 可能空跑；此时靠 get_state 兜底
        result = _final_state_result(agent, config)
        text = extract_final_text(result)
        if text:
            yield text
        else:
            try:
                result = agent.invoke(payload, config=config)
                text = extract_final_text(result if isinstance(result, dict) else {})
                if text:
                    yield text
            except Exception as exc:  # noqa: BLE001
                logger.warning("stream 无 token 后 invoke 兜底失败: %s", exc)


def _final_state_result(agent: Any, config: dict[str, Any]) -> dict[str, Any]:
    """读 checkpointer/图状态，拼成与 invoke 相近的结果字典（含 interrupt）。"""
    result: dict[str, Any] = {"messages": []}
    get_state = getattr(agent, "get_state", None)
    if not callable(get_state):
        return result
    try:
        state = get_state(config)
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


def chat(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    """发送一轮用户消息：按会话锁定的方法论 version 组装 Agent 并 invoke。"""
    conversation, agent, settings, config = _prepare_agent(
        db, user_id=user_id, thread_id=thread_id
    )
    logger.info(
        "chat user=%s thread=%s methodology=%s v%s",
        user_id,
        thread_id,
        conversation.methodology_id,
        conversation.methodology_version,
    )
    with workspace_context(user_workspace_dir(settings, user_id)):
        result = agent.invoke(
            {"messages": [{"role": "user", "content": message}]},
            config=config,
        )
    return _pack_result(
        thread_id=thread_id,
        result=result,
        methodology_id=conversation.methodology_id,
        methodology_version=conversation.methodology_version,
    )


def iter_chat_sse(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> Iterator[str]:
    """SSE：token 增量 + 最终 done（含 HITL 字段）。"""
    conversation, agent, settings, config = _prepare_agent(
        db, user_id=user_id, thread_id=thread_id
    )
    mid = conversation.methodology_id
    mver = conversation.methodology_version
    yield _sse(
        "meta",
        {
            "thread_id": thread_id,
            "methodology_id": mid,
            "methodology_version": mver,
        },
    )
    logger.info(
        "chat stream user=%s thread=%s methodology=%s v%s",
        user_id,
        thread_id,
        mid,
        mver,
    )
    assembled = ""
    try:
        with workspace_context(user_workspace_dir(settings, user_id)):
            for piece in _iter_message_tokens(
                agent,
                {"messages": [{"role": "user", "content": message}]},
                config,
            ):
                assembled += piece
                yield _sse("token", {"text": piece})
            result = _final_state_result(agent, config)
        packed = _pack_result(
            thread_id=thread_id,
            result=result,
            methodology_id=mid,
            methodology_version=mver,
        )
        if not packed["reply"] and assembled:
            packed["reply"] = assembled
        yield _sse("done", packed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat stream failed thread=%s", thread_id)
        yield _sse("error", {"message": str(exc)})


def resume_agent(
    agent: Any,
    config: dict[str, Any],
    *,
    approve: bool = True,
) -> dict[str, Any]:
    """对已编译 Agent 执行 HITL resume（CLI / API 共用）。"""
    from langgraph.types import Command

    decision_type = "approve" if approve else "reject"
    return agent.invoke(
        Command(resume={"decisions": [{"type": decision_type}]}),
        config=config,
    )


def resume_chat(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    approve: bool = True,
) -> dict[str, Any]:
    """HITL 恢复：对上次 interrupt 做 approve/reject，继续图执行。"""
    conversation, agent, settings, config = _prepare_agent(
        db, user_id=user_id, thread_id=thread_id
    )
    logger.info(
        "chat resume user=%s thread=%s decision=%s",
        user_id,
        thread_id,
        "approve" if approve else "reject",
    )
    with workspace_context(user_workspace_dir(settings, user_id)):
        result = resume_agent(agent, config, approve=approve)
    return _pack_result(
        thread_id=thread_id,
        result=result,
        methodology_id=conversation.methodology_id,
        methodology_version=conversation.methodology_version,
    )


def iter_resume_sse(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    approve: bool = True,
) -> Iterator[str]:
    """HITL 恢复的 SSE 版本。"""
    from langgraph.types import Command

    conversation, agent, settings, config = _prepare_agent(
        db, user_id=user_id, thread_id=thread_id
    )
    mid = conversation.methodology_id
    mver = conversation.methodology_version
    decision_type = "approve" if approve else "reject"
    yield _sse(
        "meta",
        {
            "thread_id": thread_id,
            "methodology_id": mid,
            "methodology_version": mver,
            "decision": decision_type,
        },
    )
    assembled = ""
    try:
        with workspace_context(user_workspace_dir(settings, user_id)):
            payload = Command(resume={"decisions": [{"type": decision_type}]})
            for piece in _iter_message_tokens(agent, payload, config):
                assembled += piece
                yield _sse("token", {"text": piece})
            result = _final_state_result(agent, config)
        packed = _pack_result(
            thread_id=thread_id,
            result=result,
            methodology_id=mid,
            methodology_version=mver,
        )
        if not packed["reply"] and assembled:
            packed["reply"] = assembled
        yield _sse("done", packed)
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat resume stream failed thread=%s", thread_id)
        yield _sse("error", {"message": str(exc)})


def get_conversation_messages(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
) -> dict[str, Any]:
    """只读：直接从 checkpointer 取历史，不编译 Agent、不物化 Skills。"""
    conversation = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")

    config = _runtime_config(user_id, thread_id)
    messages: list[Any] = []
    interrupted = False
    interrupt: str | None = None

    try:
        checkpointer = build_checkpointer(get_settings())
        cp_tuple = checkpointer.get_tuple(config)
        if cp_tuple is not None:
            checkpoint = getattr(cp_tuple, "checkpoint", None) or {}
            values = checkpoint.get("channel_values") or {}
            if isinstance(values, dict):
                messages = values.get("messages") or []
            # tasks 上挂着未处理的 interrupt → 前端展示待批准态
            for task in getattr(cp_tuple, "tasks", None) or ():
                interrupts = getattr(task, "interrupts", None) or ()
                if interrupts:
                    interrupted = True
                    interrupt = str(interrupts)
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
