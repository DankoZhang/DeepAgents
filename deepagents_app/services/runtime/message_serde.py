#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   message_serde.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   message_serde.py

LangChain / LangGraph 消息与中断载荷的前端序列化。
"""

from __future__ import annotations

from typing import Any

from deepagents_app.utils.text import normalize_message_content


def msg_role(msg: Any) -> str:
    """LangChain Message.type / OpenAI role → 前端 role。"""
    raw = getattr(msg, "type", None) or (
        msg.get("role") if isinstance(msg, dict) else None
    )
    raw = str(raw or "unknown")
    return {
        "human": "user",
        "ai": "assistant",
        "assistant": "assistant",
        "user": "user",
        "system": "system",
        "tool": "tool",
    }.get(raw, raw)


def tool_calls_payload(msg: Any) -> list[dict[str, Any]] | None:
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
        else:
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
        role = msg_role(msg)
        content = normalize_message_content(
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        name = getattr(msg, "name", None) or (
            msg.get("name") if isinstance(msg, dict) else None
        )
        tool_calls = tool_calls_payload(msg)
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
    for msg in reversed(result.get("messages") or []):
        content = normalize_message_content(
            getattr(msg, "content", None)
            or (msg.get("content") if isinstance(msg, dict) else None)
        )
        if msg_role(msg) == "assistant" and content:
            return content
    return ""


def serialize_interrupts(interrupts: Any) -> list[dict[str, Any]] | None:
    """将 LangGraph ``__interrupt__`` 转为前端可解析结构。"""
    if not interrupts:
        return None
    items = interrupts if isinstance(interrupts, (list, tuple)) else [interrupts]
    out: list[dict[str, Any]] = []
    for item in items:
        iid = getattr(item, "id", None)
        value = getattr(item, "value", item)
        raw_actions = value.get("action_requests") if isinstance(value, dict) else None
        if raw_actions is None and hasattr(value, "get"):
            try:
                raw_actions = value.get("action_requests")
            except Exception:  # noqa: BLE001
                raw_actions = None
        actions: list[dict[str, Any]] = []
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
                entry["raw"] = (
                    value
                    if isinstance(value, (dict, list, str, int, float, bool))
                    or value is None
                    else str(value)
                )
            except Exception:  # noqa: BLE001
                entry["raw"] = repr(value)
        out.append(entry)
    return out or None
