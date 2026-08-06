"""
登录鉴权
========

通过外部 Auth API 解析 Bearer token，得到合法 ``user_id``。
测试 / 本地可用 ``AUTH_DISABLED=true`` + ``AUTH_DEV_USER_ID`` 跳过外部调用。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from typing import Annotated, Any

from fastapi import Header, HTTPException

from deepagents_app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_cache: dict[str, tuple[str, float]] = {}
_cache_lock = threading.Lock()


class AuthError(Exception):
    """鉴权失败（映射为 HTTP 401）。"""


def _cache_get(token: str) -> str | None:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(token)
        if hit is None:
            return None
        user_id, expires = hit
        if expires < now:
            _cache.pop(token, None)
            return None
        return user_id


def _cache_set(token: str, user_id: str, ttl: float) -> None:
    if ttl <= 0:
        return
    with _cache_lock:
        _cache[token] = (user_id, time.monotonic() + ttl)


def clear_auth_cache() -> None:
    with _cache_lock:
        _cache.clear()


def _extract_user_id(payload: Any, field: str) -> str:
    if not isinstance(payload, dict):
        raise AuthError("鉴权响应不是 JSON 对象")
    value = payload.get(field)
    if value is None and "." in field:
        cur: Any = payload
        for part in field.split("."):
            if not isinstance(cur, dict) or part not in cur:
                raise AuthError(f"鉴权响应缺少字段：{field}")
            cur = cur[part]
        value = cur
    if value is None or str(value).strip() == "":
        raise AuthError(f"鉴权响应缺少有效 {field}")
    return str(value).strip()


def introspect_token(token: str, settings: Settings | None = None) -> str:
    """调用外部 Auth API，返回 user_id。"""
    settings = settings or get_settings()
    cached = _cache_get(token)
    if cached is not None:
        return cached

    url = (settings.auth_introspect_url or "").strip()
    if not url:
        raise AuthError("未配置 AUTH_INTROSPECT_URL")

    method = (settings.auth_introspect_method or "GET").upper()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data: bytes | None = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        data = json.dumps({"token": token}).encode("utf-8")

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=settings.auth_timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:200]
        raise AuthError(f"鉴权失败（HTTP {exc.code}）：{detail}") from exc
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"鉴权服务不可用：{exc}") from exc

    if status >= 400:
        raise AuthError(f"鉴权失败（HTTP {status}）")

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError as exc:
        raise AuthError("鉴权响应不是合法 JSON") from exc

    user_id = _extract_user_id(payload, settings.auth_user_id_field)
    _cache_set(token, user_id, float(settings.auth_cache_ttl_seconds))
    return user_id


def get_current_user_id(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """FastAPI 依赖：解析当前登录用户。"""
    settings = get_settings()
    if settings.auth_disabled:
        user_id = (settings.auth_dev_user_id or "").strip()
        if not user_id:
            raise HTTPException(
                status_code=401,
                detail="AUTH_DISABLED=true 但未配置 AUTH_DEV_USER_ID",
            )
        return user_id

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="缺少 Authorization: Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Bearer token 为空")

    try:
        return introspect_token(token, settings)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
