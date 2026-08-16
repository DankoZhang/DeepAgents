#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   constants.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   constants.py

跨模块共享常量。
"""

from __future__ import annotations

from typing import Literal, get_args

# 种子逻辑 id（实际入库主键经 ownership.scoped_id 按用户派生）
DEFAULT_MODEL_ID = "model_default"
DEMO_METHODOLOGY_ID = "demo_deepagents"

# LLM provider：schemas / Settings / 目录校验共用同一真相源
ModelProvider = Literal["openai", "anthropic", "openai_compatible"]
ALLOWED_PROVIDERS: frozenset[str] = frozenset(get_args(ModelProvider))

# MCP / HTTP 工具连通性探测超时
TOOL_PROBE_TIMEOUT_SECONDS = 15.0
