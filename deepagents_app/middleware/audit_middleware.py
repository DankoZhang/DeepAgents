"""
审计 Middleware
===============

对「敏感工具」做落盘审计，满足演示级合规需求。

审计字段：
- 时间戳
- 工具名
- 参数摘要（截断，避免日志爆炸 / 密钥泄露）
- 是否成功

生产增强方向：
- 接入 SIEM / OpenTelemetry
- 对参数做脱敏（API Key、密码）
- 与 HITL（interrupt_on）联动：先审计申请，再等人批
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langgraph.prebuilt.tool_node import ToolCallRequest

logger = logging.getLogger("deepagents_app.middleware.audit")

# 需要审计的工具名集合（可按业务扩展）
SENSITIVE_TOOLS = frozenset(
    {
        # deepagents 框架原生文件系统 / 执行工具
        "write_file",
        "edit_file",
        "execute",
        # qa-expert：笔记落盘
        "save_qa_note",
    }
)


def _audit_log_path() -> Path:
    from deepagents_app.workspace import get_workspace_root

    path = get_workspace_root() / "audit" / "tool_audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _summarize_args(args: Any, limit: int = 400) -> str:
    """把工具参数序列化为短字符串，避免完整 dump 大文件内容。"""
    try:
        text = json.dumps(args, ensure_ascii=False, default=str)
    except TypeError:
        text = str(args)
    if len(text) > limit:
        return text[:limit] + "…(truncated)"
    return text


class AuditMiddleware(AgentMiddleware):
    """把敏感工具调用追加写入 JSONL 审计文件。"""

    name = "AppAuditMiddleware"

    def _maybe_audit(self, request: ToolCallRequest, *, success: bool, error: str | None) -> None:
        tool_call = getattr(request, "tool_call", None)
        if isinstance(tool_call, dict):
            name = tool_call.get("name", "")
            args = tool_call.get("args", {})
        else:
            name = getattr(request, "name", "") or ""
            args = getattr(request, "args", {}) or {}

        if name not in SENSITIVE_TOOLS:
            return

        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": name,
            "args": _summarize_args(args),
            "success": success,
            "error": error,
        }
        line = json.dumps(record, ensure_ascii=False)
        path = _audit_log_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        logger.info("[audit] tool=%s success=%s -> %s", name, success, path)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        try:
            result = handler(request)
            self._maybe_audit(request, success=True, error=None)
            return result
        except Exception as exc:  # noqa: BLE001 — 审计后原样抛出
            self._maybe_audit(request, success=False, error=str(exc))
            raise

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        try:
            result = await handler(request)
            await asyncio.to_thread(
                self._maybe_audit, request, success=True, error=None
            )
            return result
        except Exception as exc:  # noqa: BLE001
            await asyncio.to_thread(
                self._maybe_audit, request, success=False, error=str(exc)
            )
            raise
