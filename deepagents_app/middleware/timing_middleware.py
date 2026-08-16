#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   timing_middleware.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   timing_middleware.py

计时 Middleware
===============

测量模型调用与工具调用耗时，帮助定位慢点（常见瓶颈：大模型推理、shell、检索）。

实现要点：
- 使用 ``time.perf_counter()`` 做高精度计时
- 耗时写入日志；也可扩展写入 state / metrics 后端
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langgraph.prebuilt.tool_node import ToolCallRequest

logger = logging.getLogger("deepagents_app.middleware.timing")


class TimingMiddleware(AgentMiddleware):
    """为模型与工具调用打耗时日志。"""

    name = "AppTimingMiddleware"

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        t0 = time.perf_counter()
        try:
            return handler(request)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            logger.info("[timing] model_call=%.1fms", ms)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Any],
    ) -> ModelResponse:
        t0 = time.perf_counter()
        try:
            return await handler(request)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            logger.info("[timing] model_call(async)=%.1fms", ms)

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "?") if isinstance(tool_call, dict) else "?"
        t0 = time.perf_counter()
        try:
            return handler(request)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            logger.info("[timing] tool=%s duration=%.1fms", name, ms)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Any],
    ) -> Any:
        tool_call = getattr(request, "tool_call", {}) or {}
        name = tool_call.get("name", "?") if isinstance(tool_call, dict) else "?"
        t0 = time.perf_counter()
        try:
            return await handler(request)
        finally:
            ms = (time.perf_counter() - t0) * 1000
            logger.info("[timing] tool=%s duration(async)=%.1fms", name, ms)
