"""
流式对话并发限流
==================

SSE 路由在开始流式响应前申请槽位：单 worker 用进程内 semaphore，多 worker
用 Redis 原子计数器。该模块不依赖聊天编排，可独立复用与测试。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from deepagents_app.api.errors import CapacityError
from deepagents_app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_stream_semaphore: asyncio.Semaphore | None = None
_stream_semaphore_limit: int | None = None
_REDIS_STREAM_KEY = "deepagents:chat_stream_inflight"
_REDIS_ACQUIRE_LUA = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
if n <= tonumber(ARGV[1]) then
  return n
end
redis.call('DECR', KEYS[1])
return -1
"""
_redis_slots_client: Any | None = None
_redis_slots_lock: asyncio.Lock | None = None


class StreamSlot:
    """流式槽位句柄；结束时必须 ``await release()``。"""

    async def release(self) -> None:  # noqa: B027
        return


class _LocalStreamSlot(StreamSlot):
    def __init__(self, gate: asyncio.Semaphore) -> None:
        self._gate = gate

    async def release(self) -> None:
        self._gate.release()


class _RedisStreamSlot(StreamSlot):
    def __init__(self, client: Any) -> None:
        self._client = client
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            n = await self._client.decr(_REDIS_STREAM_KEY)
            if int(n) < 0:
                await self._client.set(_REDIS_STREAM_KEY, 0)
        except Exception:  # noqa: BLE001
            logger.debug("释放 Redis 流式槽位失败", exc_info=True)


def _stream_gate(settings: Settings) -> asyncio.Semaphore | None:
    global _stream_semaphore, _stream_semaphore_limit
    limit = int(settings.chat_stream_max_concurrent)
    if limit <= 0:
        return None
    if _stream_semaphore is None or _stream_semaphore_limit != limit:
        _stream_semaphore = asyncio.Semaphore(limit)
        _stream_semaphore_limit = limit
    return _stream_semaphore


def _use_redis_stream_limiter(settings: Settings) -> bool:
    mode = settings.chat_stream_limiter.strip().lower()
    return mode == "redis" or (mode == "auto" and settings.api_workers > 1)


async def _get_redis_slots_client(settings: Settings) -> Any:
    global _redis_slots_client, _redis_slots_lock
    if _redis_slots_lock is None:
        _redis_slots_lock = asyncio.Lock()
    async with _redis_slots_lock:
        if _redis_slots_client is None:
            import redis.asyncio as aioredis

            _redis_slots_client = aioredis.from_url(
                settings.redis_url,
                socket_connect_timeout=1.5,
                socket_timeout=1.5,
            )
        return _redis_slots_client


async def close_redis_stream_slots_client() -> None:
    """关闭流式限流 Redis 客户端（lifespan 退出时调用）。"""
    global _redis_slots_client
    if _redis_slots_lock is None:
        client = _redis_slots_client
        _redis_slots_client = None
    else:
        async with _redis_slots_lock:
            client = _redis_slots_client
            _redis_slots_client = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 Redis 流式槽位客户端失败", exc_info=True)


async def _acquire_redis_stream_slot(settings: Settings, limit: int) -> StreamSlot:
    client = await _get_redis_slots_client(settings)
    timeout = float(settings.chat_stream_acquire_timeout_seconds)
    deadline = asyncio.get_running_loop().time() + (0.001 if timeout <= 0 else timeout)
    while True:
        n = int(
            await client.eval(_REDIS_ACQUIRE_LUA, 1, _REDIS_STREAM_KEY, limit, 86_400)
        )
        if n > 0:
            return _RedisStreamSlot(client)
        if asyncio.get_running_loop().time() >= deadline:
            raise CapacityError("流式对话繁忙，请稍后重试")
        await asyncio.sleep(0.05)


async def acquire_stream_slot(settings: Settings | None = None) -> StreamSlot | None:
    """申请流式槽位；不可用时抛 ``CapacityError``，不限流时返回 ``None``。"""
    settings = settings or get_settings()
    limit = int(settings.chat_stream_max_concurrent)
    if limit <= 0:
        return None
    if _use_redis_stream_limiter(settings):
        try:
            return await _acquire_redis_stream_slot(settings, limit)
        except CapacityError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("Redis 流式限流不可用，回退进程内闸门: %s", exc)
    gate = _stream_gate(settings)
    if gate is None:
        return None
    timeout = float(settings.chat_stream_acquire_timeout_seconds)
    try:
        await asyncio.wait_for(gate.acquire(), timeout=0.001 if timeout <= 0 else timeout)
    except TimeoutError as exc:
        raise CapacityError("流式对话繁忙，请稍后重试") from exc
    return _LocalStreamSlot(gate)


async def release_stream_slot(slot: StreamSlot | None) -> None:
    if slot is not None:
        await slot.release()
