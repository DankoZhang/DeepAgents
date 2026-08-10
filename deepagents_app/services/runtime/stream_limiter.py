"""
流式对话并发限流
==================

SSE 路由在开始流式响应前申请槽位：单 worker 用进程内 semaphore，多 worker
用 Redis ZSET 租约（每连接独立 member + 过期 score）。该模块不依赖聊天编排，
可独立复用与测试。

租约模型相对旧「整数计数 + 仅首次 EXPIRE」：
- 崩溃未 release 的槽位靠 score 过期，在后续 acquire 时 ZREMRANGEBYSCORE 回收
- 不会出现整 key 过期后 DECR 把计数打负
- 长连接通过 ``StreamSlot.renew()``（SSE ping）续期
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from deepagents_app.api.errors import CapacityError
from deepagents_app.config import Settings, get_settings

logger = logging.getLogger(__name__)

_stream_semaphore: asyncio.Semaphore | None = None
_stream_semaphore_limit: int | None = None

# ZSET：member=slot_id，score=unix 过期时间
_REDIS_LEASE_KEY = "deepagents:chat_stream_leases"
# 旧计数器 key；启动/acquire 时顺带删掉，避免残留干扰运维观察
_REDIS_LEGACY_COUNTER_KEY = "deepagents:chat_stream_inflight"
# 租约时长；短于长 SSE 时靠 renew（ping）续期，崩溃后最多泄漏该窗口
_REDIS_LEASE_TTL_SECONDS = 90.0

_REDIS_ACQUIRE_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local limit = tonumber(ARGV[2])
local expire_at = tonumber(ARGV[3])
local member = ARGV[4]
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
local n = redis.call('ZCARD', key)
if n >= limit then
  return -1
end
redis.call('ZADD', key, expire_at, member)
return 1
"""

_REDIS_RENEW_LUA = """
local key = KEYS[1]
local member = ARGV[1]
local expire_at = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
if redis.call('ZSCORE', key, member) == false then
  return 0
end
redis.call('ZADD', key, expire_at, member)
-- 顺带清过期，控制 ZSET 体积
redis.call('ZREMRANGEBYSCORE', key, '-inf', now)
return 1
"""

_redis_slots_client: Any | None = None
_redis_slots_lock: asyncio.Lock | None = None
_legacy_counter_cleared = False


class StreamSlot:
    """流式槽位句柄；结束时必须 ``await release()``。"""

    async def release(self) -> None:  # noqa: B027
        return

    async def renew(self) -> None:  # noqa: B027
        """长连接续租；本地 semaphore 无操作。"""
        return


class _LocalStreamSlot(StreamSlot):
    def __init__(self, gate: asyncio.Semaphore) -> None:
        self._gate = gate

    async def release(self) -> None:
        self._gate.release()


class _RedisStreamSlot(StreamSlot):
    def __init__(self, client: Any, slot_id: str, *, lease_ttl: float) -> None:
        self._client = client
        self._slot_id = slot_id
        self._lease_ttl = lease_ttl
        self._released = False

    async def renew(self) -> None:
        if self._released:
            return
        now = time.time()
        try:
            await self._client.eval(
                _REDIS_RENEW_LUA,
                1,
                _REDIS_LEASE_KEY,
                self._slot_id,
                now + self._lease_ttl,
                now,
            )
        except Exception:  # noqa: BLE001
            logger.debug("续期 Redis 流式槽位失败", exc_info=True)

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            await self._client.zrem(_REDIS_LEASE_KEY, self._slot_id)
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
    global _redis_slots_client, _redis_slots_lock, _legacy_counter_cleared
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
        client = _redis_slots_client
        if not _legacy_counter_cleared:
            try:
                await client.delete(_REDIS_LEGACY_COUNTER_KEY)
            except Exception:  # noqa: BLE001
                logger.debug("清理旧流式计数器 key 失败", exc_info=True)
            _legacy_counter_cleared = True
        return client


async def close_redis_stream_slots_client() -> None:
    """关闭流式限流 Redis 客户端（lifespan 退出时调用）。"""
    global _redis_slots_client, _legacy_counter_cleared
    if _redis_slots_lock is None:
        client = _redis_slots_client
        _redis_slots_client = None
    else:
        async with _redis_slots_lock:
            client = _redis_slots_client
            _redis_slots_client = None
    _legacy_counter_cleared = False
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 Redis 流式槽位客户端失败", exc_info=True)


async def _acquire_redis_stream_slot(settings: Settings, limit: int) -> StreamSlot:
    client = await _get_redis_slots_client(settings)
    timeout = float(settings.chat_stream_acquire_timeout_seconds)
    deadline = asyncio.get_running_loop().time() + (0.001 if timeout <= 0 else timeout)
    lease_ttl = _REDIS_LEASE_TTL_SECONDS
    while True:
        slot_id = uuid.uuid4().hex
        now = time.time()
        ok = int(
            await client.eval(
                _REDIS_ACQUIRE_LUA,
                1,
                _REDIS_LEASE_KEY,
                now,
                limit,
                now + lease_ttl,
                slot_id,
            )
        )
        if ok > 0:
            return _RedisStreamSlot(client, slot_id, lease_ttl=lease_ttl)
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
