"""
出站 URL 安全校验（防 SSRF）
============================

限制服务端主动发起的 HTTP(S) 请求目标：禁止环回、私网、link-local 与
保留地址。用于模型连通性测试的 ``base_url`` 与 MCP 远程传输的 ``url``。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from deepagents_app.api.errors import BusinessError


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_http_url(
    url: str,
    *,
    label: str = "url",
    allow_private: bool = False,
) -> str:
    """
    校验 URL 可被服务端安全访问；失败抛 ``BusinessError``。

    - 仅允许 ``http`` / ``https``
    - 默认拒绝环回/私网/link-local（防 SSRF）
    - ``allow_private=True`` 时仅校验 scheme/主机形态（本地 Ollama 等）
    """
    text = (url or "").strip()
    if not text:
        raise BusinessError(f"{label} 不能为空")

    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise BusinessError(f"{label} 仅允许 http/https，收到：{scheme or '(空)'}")
    host = parsed.hostname
    if not host:
        raise BusinessError(f"{label} 缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise BusinessError(f"{label} 不允许内嵌用户名/密码")

    if allow_private:
        return text

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _is_blocked_ip(literal):
            raise BusinessError(f"{label} 禁止访问内网/环回地址")
        return text

    try:
        infos = socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BusinessError(f"{label} 主机名无法解析：{host}") from exc
    if not infos:
        raise BusinessError(f"{label} 主机名无法解析：{host}")

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        if _is_blocked_ip(ip):
            raise BusinessError(f"{label} 解析到内网/环回地址，已拒绝")
    return text
