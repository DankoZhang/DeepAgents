"""
日志 Middleware
===============

在模型调用与工具调用前后打印结构化日志，便于调试「Agent 为什么这么做」。

钩子说明（LangChain AgentMiddleware）：
- ``before_agent`` / ``abefore_agent``：Agent 开始执行前（含子 Agent）
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
from langchain_core.runnables import RunnableConfig
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.runtime import Runtime

logger = logging.getLogger("deepagents_app.middleware.logging")


def _agent_name_from_config(config: RunnableConfig | None) -> str | None:
    """从 RunnableConfig 提取 Agent 名。"""
    if not config:
        return None
    meta = config.get("metadata") or {}
    name = meta.get("lc_agent_name") or config.get("run_name")
    return str(name) if name else None


def _resolve_agent_name(
    runtime: Runtime | None = None,
    *,
    config: RunnableConfig | None = None,
) -> str:
    """从 RunnableConfig / Runtime 解析当前 Agent 名（主 Agent 或子 Agent）。

    优先使用显式传入的 ``config``；若无则回退到上下文中的 ``get_config()``
    （``wrap_model_call`` / ``wrap_tool_call`` 无法注入 config 时使用）。
    """
    name = _agent_name_from_config(config)
    if name:
        return name

    try:
        from langgraph.config import get_config

        name = _agent_name_from_config(get_config())
        if name:
            return name
    except RuntimeError:
        pass

    if runtime is not None:
        context = getattr(runtime, "context", None)
        for attr in ("agent_name", "name", "lc_agent_name"):
            value = getattr(context, attr, None) if context is not None else None
            if value:
                return str(value)
    return "?"


def _tool_call_name(request: ToolCallRequest) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    name = tool_call.get("name") if isinstance(tool_call, dict) else None
    # ToolCallRequest 在不同版本字段略有差异，做兼容读取
    if name is None:
        name = getattr(request, "name", None) or getattr(request, "tool_name", "?")
    return str(name or "?")


def _task_subagent_type(request: ToolCallRequest) -> str | None:
    """若工具为 ``task``，提取即将委派的子 Agent 类型。"""
    tool_call = getattr(request, "tool_call", {}) or {}
    if not isinstance(tool_call, dict) or tool_call.get("name") != "task":
        return None
    args = tool_call.get("args") or {}
    if not isinstance(args, dict):
        return None
    subagent = args.get("subagent_type") or args.get("agent")
    return str(subagent) if subagent else None


class LoggingMiddleware(AgentMiddleware):
    """记录模型 / 工具调用的输入摘要与结果类型。"""

    # 自定义 name，避免与 deepagents 默认中间件冲突（冲突会触发替换语义）
    name = "AppLoggingMiddleware"

    def before_agent(
        self,
        state: Any,
        runtime: Runtime,
        *,
        config: RunnableConfig,
    ) -> dict[str, Any] | None:
        # config 由 LangGraph RunnableCallable 按参数名/类型注入
        agent = _resolve_agent_name(runtime, config=config)
        n_msgs = len((state or {}).get("messages") or []) if isinstance(state, dict) else "?"
        logger.info("[agent] ▶ name=%s messages=%s", agent, n_msgs)
        return None

    async def abefore_agent(
        self,
        state: Any,
        runtime: Runtime,
        *,
        config: RunnableConfig,
    ) -> dict[str, Any] | None:
        agent = _resolve_agent_name(runtime, config=config)
        n_msgs = len((state or {}).get("messages") or []) if isinstance(state, dict) else "?"
        logger.info("[agent:async] ▶ name=%s messages=%s", agent, n_msgs)
        return None

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        agent = _resolve_agent_name(getattr(request, "runtime", None))
        n_msgs = len(request.messages or [])
        n_tools = len(request.tools or [])
        logger.info("[model] ▶ agent=%s messages=%d tools=%d", agent, n_msgs, n_tools)
        response = handler(request)
        logger.info("[model] ◀ agent=%s response_type=%s", agent, type(response).__name__)
        return response

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        agent = _resolve_agent_name(getattr(request, "runtime", None))
        n_msgs = len(request.messages or [])
        n_tools = len(request.tools or [])
        logger.info("[model:async] ▶ agent=%s messages=%d tools=%d", agent, n_msgs, n_tools)
        response = await handler(request)
        logger.info(
            "[model:async] ◀ agent=%s response_type=%s",
            agent,
            type(response).__name__,
        )
        return response

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        name = _tool_call_name(request)
        agent = _resolve_agent_name()
        subagent = _task_subagent_type(request)
        if subagent:
            logger.info("[tool] ▶ agent=%s name=%s -> subagent=%s", agent, name, subagent)
        else:
            logger.info("[tool] ▶ agent=%s name=%s", agent, name)
        result = handler(request)
        logger.info("[tool] ◀ agent=%s name=%s done", agent, name)
        return result

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        name = _tool_call_name(request)
        agent = _resolve_agent_name()
        subagent = _task_subagent_type(request)
        if subagent:
            logger.info(
                "[tool:async] ▶ agent=%s name=%s -> subagent=%s",
                agent,
                name,
                subagent,
            )
        else:
            logger.info("[tool:async] ▶ agent=%s name=%s", agent, name)
        result = await handler(request)
        logger.info("[tool:async] ◀ agent=%s name=%s done", agent, name)
        return result
