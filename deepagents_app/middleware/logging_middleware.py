"""
日志 Middleware
===============

在模型调用与工具调用前后打印结构化日志，便于调试「Agent 为什么这么做」。

钩子说明（LangChain AgentMiddleware）：
- ``wrap_model_call`` / ``awrap_model_call``：包裹一次 LLM 调用
- ``wrap_tool_call`` / ``awrap_tool_call``：包裹一次工具执行
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langgraph.prebuilt.tool_node import ToolCallRequest

logger = logging.getLogger("deepagents_app.middleware.logging")


class LoggingMiddleware(AgentMiddleware):
    """记录模型 / 工具调用的输入摘要与结果类型。"""

    # 自定义 name，避免与 deepagents 默认中间件冲突（冲突会触发替换语义）
    name = "AppLoggingMiddleware"

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        n_msgs = len(request.messages or [])
        n_tools = len(request.tools or [])
        logger.info("[model] ▶ messages=%d tools=%d", n_msgs, n_tools)
        response = handler(request)
        logger.info("[model] ◀ response_type=%s", type(response).__name__)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        n_msgs = len(request.messages or [])
        n_tools = len(request.tools or [])
        logger.info("[model:async] ▶ messages=%d tools=%d", n_msgs, n_tools)
        response = await handler(request)
        logger.info("[model:async] ◀ response_type=%s", type(response).__name__)
        return response

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        tool_name = getattr(request, "tool_call", {}) or {}
        name = tool_name.get("name") if isinstance(tool_name, dict) else None
        # ToolCallRequest 在不同版本字段略有差异，做兼容读取
        if name is None:
            name = getattr(request, "name", None) or getattr(request, "tool_name", "?")
        logger.info("[tool] ▶ name=%s", name)
        result = handler(request)
        logger.info("[tool] ◀ name=%s done", name)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        tool_name = getattr(request, "tool_call", {}) or {}
        name = tool_name.get("name") if isinstance(tool_name, dict) else None
        if name is None:
            name = getattr(request, "name", None) or getattr(request, "tool_name", "?")
        logger.info("[tool:async] ▶ name=%s", name)
        result = await handler(request)
        logger.info("[tool:async] ◀ name=%s done", name)
        return result
