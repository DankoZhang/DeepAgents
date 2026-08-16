#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   __init__.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   __init__.py

DeepAgents 方法论平台
====================

基于 LangChain ``deepagents`` 的可配置多 Agent 后端。

平台入口：``python server.py``（或 uvicorn ``deepagents_app.api.app:app``）。
用户种子由 ``POST /api/bootstrap`` 幂等写入。
"""

__version__ = "0.2.0"
