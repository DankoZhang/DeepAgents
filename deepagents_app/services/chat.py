"""
聊天服务
========

按 thread 加载 Conversation → 校验 user_id → Agent Factory →
invoke / resume；历史消息直接读 checkpointer，不触发 Agent 编译。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import NotFoundError
from deepagents_app.config import get_settings
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id
from deepagents_app.services.agent_factory import build_agent_from_methodology
from deepagents_app.services.conversation import get_conversation_by_thread
from deepagents_app.utils.text import normalize_message_content

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
    return {
        "configurable": {
            "thread_id": checkpoint_thread_id(user_id, thread_id),
        }
    }


def chat(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    conversation = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")

    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        owner_user_id=user_id,
        version=conversation.methodology_version,
    )
    config = _runtime_config(user_id, thread_id)
    logger.info(
        "chat user=%s thread=%s methodology=%s v%s",
        user_id,
        thread_id,
        conversation.methodology_id,
        conversation.methodology_version,
    )
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


def resume_chat(
    db: Session,
    *,
    user_id: str,
    thread_id: str,
    approve: bool = True,
) -> dict[str, Any]:
    from langgraph.types import Command

    conversation = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if conversation is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")

    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        owner_user_id=user_id,
        version=conversation.methodology_version,
    )
    config = _runtime_config(user_id, thread_id)
    decision_type = "approve" if approve else "reject"
    logger.info(
        "chat resume user=%s thread=%s decision=%s",
        user_id,
        thread_id,
        decision_type,
    )
    result = agent.invoke(
        Command(resume={"decisions": [{"type": decision_type}]}),
        config=config,
    )
    return _pack_result(
        thread_id=thread_id,
        result=result,
        methodology_id=conversation.methodology_id,
        methodology_version=conversation.methodology_version,
    )


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
