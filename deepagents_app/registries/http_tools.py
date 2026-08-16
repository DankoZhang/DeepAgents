#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   http_tools.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   http_tools.py

HTTP 工具运行时
===============

把 ToolDefinition.config 转成 LangChain StructuredTool：
模型填 input_schema 字段 → 按 param_in 映射 → httpx 调用外部 API。

进程内复用一个 ``AsyncClient``（lifespan 启停），按请求覆盖 timeout。
"""

from __future__ import annotations

import threading
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from langchain_core.tools import StructuredTool

from deepagents_app.constants import TOOL_PROBE_TIMEOUT_SECONDS
from deepagents_app.db.models import ToolDefinition
from deepagents_app.utils.http_tool_safety import (
    fill_path_placeholders,
    url_has_placeholder,
    validate_http_tool_config,
)

MAX_RESPONSE_CHARS = 50_000

_http_client: httpx.AsyncClient | None = None
_http_client_lock = threading.Lock()


def _new_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False)


def init_http_tool_client() -> None:
    """在 lifespan 启动时建立共享连接池。"""
    get_http_tool_client()


def get_http_tool_client() -> httpx.AsyncClient:
    """返回共享客户端；未初始化时惰性创建（测试 / 脚本）。"""
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = _new_http_client()
        return _http_client


async def close_http_tool_client() -> None:
    """关闭共享客户端（lifespan 退出时调用）。"""
    global _http_client
    with _http_client_lock:
        client = _http_client
        _http_client = None
    if client is not None:
        await client.aclose()


def load_http_tool(tool_def: ToolDefinition) -> StructuredTool:
    """一条 HTTP ToolDefinition → 一个 StructuredTool。"""
    if tool_def.status != "active":
        raise ValueError(f"工具已禁用：{tool_def.name}")
    cfg = validate_http_tool_config(dict(tool_def.config or {}))
    known = set((cfg["input_schema"].get("properties") or {}).keys())

    async def call_http(**arguments: Any) -> str:
        payload = {k: v for k, v in arguments.items() if k in known}
        return await execute_http_tool(cfg, payload)

    return StructuredTool(
        name=tool_def.name,
        description=tool_def.description or "",
        args_schema=cfg["input_schema"],
        coroutine=call_http,
    )


async def execute_http_tool(cfg: dict[str, Any], arguments: dict[str, Any]) -> str:
    """按规范化 config 发 HTTP 请求，返回截断后的文本（失败也以字符串回给模型）。"""
    method = str(cfg["method"])
    url = str(cfg["url"])
    param_in: dict[str, str] = dict(cfg.get("param_in") or {})
    static_headers = dict(cfg.get("headers") or {})
    timeout = float(cfg.get("timeout") or 15)

    query: dict[str, Any] = {}
    body: dict[str, Any] = {}
    header_params: dict[str, str] = {}
    for name, loc in param_in.items():
        if name not in arguments or arguments[name] is None:
            continue
        value = arguments[name]
        if loc == "path":
            url = url.replace("{" + name + "}", quote(str(value), safe=""))
        elif loc == "query":
            query[name] = value
        elif loc == "body":
            body[name] = value
        elif loc == "header":
            header_params[name] = str(value)

    if url_has_placeholder(url):
        return "HTTP 请求失败：URL 仍有未替换的路径参数"
    original_host = urlparse(str(cfg["url"])).hostname
    if urlparse(url).hostname != original_host:
        return "HTTP 请求失败：URL 主机名被篡改"

    headers = {**header_params, **static_headers}
    try:
        resp = await get_http_tool_client().request(
            method,
            url,
            params=query or None,
            json=body or None,
            headers=headers or None,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        return f"HTTP 请求失败：{exc}"

    text = resp.text or ""
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + "\n...[truncated]"
    if resp.status_code >= 400:
        return f"HTTP {resp.status_code}: {text}"
    return text or f"HTTP {resp.status_code} (empty body)"


async def probe_http_connection(cfg: dict[str, Any]) -> dict[str, Any]:
    """
    探测 HTTP 工具是否可达（不按业务参数调用）。

    POST/PUT/PATCH 改用 GET，避免试连产生写入副作用。
    任意 HTTP 响应（含 4xx/5xx）视为网络连通。
    """
    cfg = validate_http_tool_config(dict(cfg or {}))
    url = fill_path_placeholders(str(cfg["url"]))
    method = str(cfg["method"])
    probe_method = "GET" if method in {"POST", "PUT", "PATCH"} else method
    timeout = min(float(cfg.get("timeout") or TOOL_PROBE_TIMEOUT_SECONDS), TOOL_PROBE_TIMEOUT_SECONDS)
    headers = dict(cfg.get("headers") or {})
    try:
        resp = await get_http_tool_client().request(
            probe_method,
            url,
            headers=headers or None,
            timeout=timeout,
        )
    except httpx.RequestError as exc:
        return {
            "ok": False,
            "message": "连通性测试失败",
            "detail": str(exc)[:300],
        }
    note = ""
    if probe_method != method:
        note = f"；已用 {probe_method} 探测以免 {method} 写入"
    return {
        "ok": True,
        "message": "连通性测试成功",
        "detail": f"HTTP {resp.status_code} {probe_method} {url}{note}",
    }
