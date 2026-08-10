"""
共享 Redis 客户端
=================

进程内单例的 ``redis.asyncio`` 客户端，供不需要独立连接参数的轻量用途复用
（当前：GC 调度器的全局单飞锁）。

流式限流（``services.chat``）与缓存失效广播（``services.cache_pubsub``：Agent / MCP）目前
各自维护客户端，后续可迁移到本模块统一管理。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

_client: Any | None = None
_lock = asyncio.Lock()


async def get_shared_redis() -> Any:
    """惰性创建并返回进程内共享客户端。"""
    global _client
    async with _lock:
        if _client is None:
            import redis.asyncio as aioredis

            from deepagents_app.config import get_settings

            _client = aioredis.from_url(
                get_settings().redis_url,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        return _client


async def close_shared_redis() -> None:
    """关闭共享客户端（lifespan 退出时调用）。"""
    global _client
    async with _lock:
        client, _client = _client, None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭共享 Redis 客户端失败", exc_info=True)
