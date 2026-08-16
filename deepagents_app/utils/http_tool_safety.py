#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   http_tool_safety.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   http_tool_safety.py

HTTP 工具配置校验
================

- ``url``：SSRF 校验（与 MCP HTTP 传输同一套）
- 主机名禁止占位符，避免模型改写目标主机
- ``input_schema`` 必须是 JSON Schema object
- 补齐 ``param_in``（URL ``{name}`` → path；其余按 method 默认）
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from deepagents_app.api.errors import BusinessError
from deepagents_app.config import Settings, get_settings
from deepagents_app.utils.url_safety import assert_safe_http_url

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}
_PARAM_IN = {"path", "query", "body", "header"}
MIN_TIMEOUT = 1.0
MAX_TIMEOUT = 60.0


def url_has_placeholder(url: str) -> bool:
    """URL 是否仍含 ``{name}`` 形态的 path 占位符。"""
    return bool(_PLACEHOLDER_RE.search(url))


def fill_path_placeholders(url: str, value: str = "ping") -> str:
    """把 URL 中的 ``{name}`` 替换为探测用占位值。"""
    return _PLACEHOLDER_RE.sub(value, url)


def validate_http_tool_config(
    cfg: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """校验并返回规范化后的 HTTP 工具 config（浅拷贝）。"""
    settings = settings or get_settings()
    out = dict(cfg or {})

    method = str(out.get("method") or "GET").strip().upper()
    if method not in _HTTP_METHODS:
        raise BusinessError(f"不支持的 HTTP method：{method}")
    out["method"] = method

    url = str(out.get("url") or "").strip()
    if not url:
        raise BusinessError("http 工具需要提供 url")
    parsed = urlparse(url)
    host = parsed.hostname
    if not host:
        raise BusinessError("HTTP tool url 缺少主机名")
    if "{" in host or "}" in host:
        raise BusinessError("HTTP tool url 的主机名不能包含占位符")
    probe = _PLACEHOLDER_RE.sub("placeholder", url)
    assert_safe_http_url(
        probe,
        label="HTTP tool url",
        allow_private=bool(settings.auth_disabled),
    )
    out["url"] = url

    schema = out.get("input_schema")
    if not isinstance(schema, dict):
        raise BusinessError("http 工具需要 input_schema 对象")
    schema = dict(schema)
    if schema.get("type") != "object":
        raise BusinessError("input_schema.type 必须是 object")
    props = schema.get("properties")
    if not isinstance(props, dict):
        raise BusinessError("input_schema.properties 必须是对象")
    out["input_schema"] = schema

    raw_param_in = out.get("param_in") or {}
    if not isinstance(raw_param_in, dict):
        raise BusinessError("param_in 必须是对象")
    param_in: dict[str, str] = {}
    for key, value in raw_param_in.items():
        loc = str(value).strip().lower()
        if loc not in _PARAM_IN:
            raise BusinessError(f"param_in.{key} 非法：{value}")
        name = str(key)
        if name not in props:
            raise BusinessError(f"param_in 含未知参数：{name}")
        param_in[name] = loc

    path_names = _PLACEHOLDER_RE.findall(parsed.path or "")
    for name in path_names:
        if name not in props:
            raise BusinessError(
                f"URL 占位符 {{{name}}} 未在 input_schema.properties 中声明"
            )
        if name in param_in and param_in[name] != "path":
            raise BusinessError(f"URL 占位符 {{{name}}} 必须是 path 参数")
        param_in.setdefault(name, "path")

    default_loc = "query" if method in {"GET", "DELETE"} else "body"
    for name in props:
        param_in.setdefault(str(name), default_loc)

    if method in {"GET", "DELETE"}:
        body_params = [name for name, loc in param_in.items() if loc == "body"]
        if body_params:
            raise BusinessError(
                f"{method} 请求不能使用 body 参数：{', '.join(body_params)}"
            )
    out["param_in"] = param_in

    headers = out.get("headers") or {}
    if not isinstance(headers, dict):
        raise BusinessError("headers 必须是对象")
    out["headers"] = {str(k): str(v) for k, v in headers.items()}

    timeout = out.get("timeout", 15)
    try:
        timeout_val = float(timeout)
    except (TypeError, ValueError) as exc:
        raise BusinessError("timeout 必须是数字") from exc
    if timeout_val < MIN_TIMEOUT or timeout_val > MAX_TIMEOUT:
        raise BusinessError(f"timeout 须在 {MIN_TIMEOUT:g}–{MAX_TIMEOUT:g} 秒")
    out["timeout"] = timeout_val
    return out
