"""
登录鉴权
========

通过外部 Auth API 解析 Bearer token，得到合法 ``user_id``。
测试 / 本地可用 ``AUTH_DISABLED=true`` + ``AUTH_DEV_USER_ID`` 跳过外部调用。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections import OrderedDict
from typing import Annotated, Any

import httpx
from fastapi import Header, HTTPException

from deepagents_app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# token 摘要 → (user_id, expires_monotonic)；容量有上限的 TTL LRU
_AUTH_CACHE_MAX_SIZE = 1024
_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_cache_lock = threading.Lock()

_http_client: httpx.AsyncClient | None = None
_http_client_lock = threading.Lock()


class AuthError(Exception):
    """鉴权失败（映射为 HTTP 401）。"""


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(token: str) -> str | None:
    key = _token_digest(token)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        user_id, expires = hit
        if expires < now:
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return user_id


def _cache_set(token: str, user_id: str, ttl: float) -> None:
    if ttl <= 0:
        return
    key = _token_digest(token)
    with _cache_lock:
        _cache[key] = (user_id, time.monotonic() + ttl)
        _cache.move_to_end(key)
        while len(_cache) > _AUTH_CACHE_MAX_SIZE:
            _cache.popitem(last=False)


def clear_auth_cache() -> None:
    with _cache_lock:
        _cache.clear()


async def close_auth_http_client() -> None:
    """关闭进程内复用的 httpx 客户端（lifespan 退出时调用）。"""
    global _http_client
    with _http_client_lock:
        client = _http_client
        _http_client = None
    if client is not None:
        await client.aclose()


def _get_http_client() -> httpx.AsyncClient:
    """返回复用连接池；超时在每次请求上读取当前 Settings。"""
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.AsyncClient()
        return _http_client


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


async def introspect_token(token: str, settings: Settings | None = None) -> str:
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
    json_body: dict[str, str] | None = None
    if method == "POST":
        headers["Content-Type"] = "application/json"
        json_body = {"token": token}

    client = _get_http_client()
    try:
        resp = await client.request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=float(settings.auth_timeout_seconds),
        )
    except Exception as exc:  # noqa: BLE001
        raise AuthError(f"鉴权服务不可用：{exc}") from exc

    if resp.status_code >= 400:
        logger.warning(
            "鉴权失败 HTTP %s：%s",
            resp.status_code,
            (resp.text or "")[:200],
        )
        raise AuthError("鉴权失败")

    try:
        payload = resp.json() if resp.content else {}
    except ValueError as exc:
        raise AuthError("鉴权响应不是合法 JSON") from exc

    # RFC 7662：active=false 表示 token 无效/已吊销；缺省字段则依赖服务端 4xx
    if isinstance(payload, dict) and "active" in payload and payload.get("active") is not True:
        raise AuthError("token 无效或已吊销")
    if isinstance(payload, dict) and "exp" in payload:
        try:
            if float(payload["exp"]) < time.time():
                raise AuthError("token 已过期")
        except (TypeError, ValueError) as exc:
            raise AuthError("token exp 字段非法") from exc

    user_id = _extract_user_id(payload, settings.auth_user_id_field)
    _cache_set(token, user_id, float(settings.auth_cache_ttl_seconds))
    return user_id


async def get_current_user_id(
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
        return await introspect_token(token, settings)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
