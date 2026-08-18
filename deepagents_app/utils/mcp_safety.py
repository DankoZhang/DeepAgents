#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   mcp_safety.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   mcp_safety.py

MCP 连接配置安全校验
====================

- ``stdio``：默认禁用；开启后仍须命令白名单（防任意命令执行）
- ``sse`` / ``streamable_http``：对 ``url`` 做 SSRF 校验
"""

from __future__ import annotations

import shlex
from typing import Any

from deepagents_app.api.errors import BusinessError
from deepagents_app.config import Settings, get_settings
from deepagents_app.utils.url_safety import assert_safe_http_url


def _allowlist(settings: Settings) -> set[str]:
    raw = settings.mcp_stdio_command_allowlist or ""
    return {part.strip() for part in raw.split(",") if part.strip()}


def _command_basename(command: str) -> str:
    # 允许 "npx" 或 "/usr/bin/npx"；取最后一段 basename
    text = (command or "").strip()
    if not text:
        return ""
    # 拒绝 shell 拼接痕迹
    if any(ch in text for ch in (";", "|", "&", "`", "$", "\n", "\r")):
        raise BusinessError("MCP stdio command 含非法字符")
    parts = shlex.split(text, posix=True)
    if not parts:
        return ""
    token = parts[0]
    if "/" in token or "\\" in token:
        token = token.replace("\\", "/").rsplit("/", 1)[-1]
    return token


def validate_mcp_config(
    cfg: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """校验并返回规范化后的 MCP config（浅拷贝）。"""
    settings = settings or get_settings()
    out = dict(cfg or {})
    # 与 McpServerConfig 默认一致；缺字段不回退 stdio（stdio 默认禁用）
    transport = str(out.get("transport") or "streamable_http").strip().lower()
    out["transport"] = transport

    if transport == "stdio":
        if not settings.mcp_stdio_enabled:
            raise BusinessError(
                "MCP stdio 传输已禁用（安全策略）。"
                "请使用 sse/streamable_http，或设置 MCP_STDIO_ENABLED=true 并配置命令白名单"
            )
        command = str(out.get("command") or "").strip()
        if not command:
            raise BusinessError("stdio 传输需要提供 command")
        basename = _command_basename(command)
        allowed = _allowlist(settings)
        if not allowed:
            raise BusinessError(
                "已启用 MCP stdio，但 MCP_STDIO_COMMAND_ALLOWLIST 为空；拒绝执行"
            )
        if basename not in allowed:
            raise BusinessError(
                f"MCP stdio 命令不在白名单：{basename or command}；"
                f"允许：{', '.join(sorted(allowed))}"
            )
        out["command"] = command
    elif transport in {"sse", "streamable_http"}:
        url = str(out.get("url") or "").strip()
        if not url:
            raise BusinessError(f"{transport} 传输需要提供 url")
        # 生产拦截私网；本地 AUTH_DISABLED 时允许连本机 MCP
        out["url"] = assert_safe_http_url(
            url,
            label="MCP url",
            allow_private=bool(settings.auth_disabled),
        )
    else:
        raise BusinessError(f"不支持的 MCP transport：{transport}")

    return out
