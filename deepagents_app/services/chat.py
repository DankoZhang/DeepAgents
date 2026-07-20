"""
聊天服务
========

按 thread 加载 Conversation → Agent Factory（锁定 methodology_version）→
invoke / resume / 读取 checkpointer 历史。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.services.agent_factory import build_agent_from_methodology
from deepagents_app.services.conversation import get_conversation_by_thread

logger = logging.getLogger(__name__)


def _normalize_content(content: Any) -> str:
    """统一把 str / multimodal block 列表转为纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        ]
        return "\n".join(t for t in texts if t)
    return str(content)


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
        content = _normalize_content(
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        name = getattr(msg, "name", None) or (
            msg.get("name") if isinstance(msg, dict) else None
        )
        # 跳过空内容的中间 tool / 系统噪声（保留有文本的）
        if not content and role not in {"user", "assistant"}:
            continue
        out.append({"role": role, "content": content, "name": name})
    return out


def extract_final_text(result: dict[str, Any]) -> str:
    """从 agent.invoke 结果取出最后一条 AI 文本。"""
    messages = result.get("messages") or []
    for msg in reversed(messages):
        role = _msg_role(msg)
        content = _normalize_content(
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


def chat(
    db: Session,
    *,
    thread_id: str,
    message: str,
) -> dict[str, Any]:
    """
    执行一轮对话。

    Returns:
        {
          "thread_id": ...,
          "reply": "...",
          "interrupted": bool,
          "interrupt": ...,
          "methodology_id": ...,
          "methodology_version": ...,
        }
    """
    conversation = get_conversation_by_thread(db, thread_id)
    if conversation is None:
        raise LookupError(f"会话不存在：thread_id={thread_id}")

    # 必须按会话创建时的 version 构建，避免方法论升级影响进行中的对话
    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        version=conversation.methodology_version,
    )
    # LangGraph 用 thread_id 隔离多轮 checkpointer 状态
    config = {"configurable": {"thread_id": thread_id}}
    logger.info(
        "chat thread=%s methodology=%s v%s",
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
    thread_id: str,
    approve: bool = True,
) -> dict[str, Any]:
    """
    恢复 HITL 中断的会话。

    approve=True → 批准当前工具调用并继续；
    approve=False → 拒绝并结束本轮。
    """
    from langgraph.types import Command

    conversation = get_conversation_by_thread(db, thread_id)
    if conversation is None:
        raise LookupError(f"会话不存在：thread_id={thread_id}")

    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        version=conversation.methodology_version,
    )
    config = {"configurable": {"thread_id": thread_id}}
    decision_type = "approve" if approve else "reject"
    logger.info("chat resume thread=%s decision=%s", thread_id, decision_type)
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
    thread_id: str,
) -> dict[str, Any]:
    """
    从 checkpointer 读取会话历史（供前端聊天页回放）。

    无历史时返回空 messages 列表。
    """
    conversation = get_conversation_by_thread(db, thread_id)
    if conversation is None:
        raise LookupError(f"会话不存在：thread_id={thread_id}")

    agent = build_agent_from_methodology(
        db,
        conversation.methodology_id,
        version=conversation.methodology_version,
    )
    config = {"configurable": {"thread_id": thread_id}}
    messages: list[Any] = []
    interrupted = False
    interrupt: str | None = None

    try:
        state = agent.get_state(config)
        values = getattr(state, "values", None) or {}
        if isinstance(values, dict):
            messages = values.get("messages") or []
        # tasks 上挂着未解决的 interrupt（HITL 暂停中）
        tasks = getattr(state, "tasks", None) or ()
        for task in tasks:
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
