"""
周期性后台任务辅助
==================

供 Skills / content_blob 等 GC 调度器复用：按间隔跑协程，可优雅停止。
多 worker 部署下用 ``single_flight`` 包一层，保证每个间隔窗口全集群只跑一次。
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

Tick = Callable[[], Awaitable[None]]

_WORKER_ID = uuid.uuid4().hex
_RENEW_LEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('EXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""


async def _renew_lease(
    client: Any,
    *,
    key: str,
    token: str,
    ttl_s: int,
) -> None:
    """任务尚在运行时续期，只允许锁的持有者刷新自己的 lease。"""
    while True:
        # TTL 正常以小时计；1 秒仅用于测试/极端配置，也必须在过期前续租。
        await asyncio.sleep(max(0.05, ttl_s / 3))
        try:
            renewed = await client.eval(_RENEW_LEASE_LUA, 1, key, token, ttl_s)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 全局锁续期失败：%s", key, exc)
            return
        if not renewed:
            logger.warning("%s 全局锁已不属于当前 worker，停止续期", key)
            return


def single_flight(key: str, ttl_s: float, tick: Tick) -> Tick:
    """
    把 ``tick`` 包成「每个 TTL 窗口内全集群只执行一次」。

    抢到锁后不主动释放：靠 TTL 过期天然限流。若结束时 ``DEL``，同一窗口里
    另一个 worker 的 tick 会紧接着再跑一遍，等于没加锁。运行中的任务会续租，
    防止耗时异常长的 GC 在锁过期后与另一 worker 的新一轮 GC 重叠。
    ``ttl_s`` 取间隔的 0.9 倍即可，既保证一个窗口一次，也能吸收 worker
    重启造成的定时器漂移。

    Redis 不可用时跳过本轮：GC 可延期，且此时不应放多进程并发删除出去。
    """

    async def _guarded() -> None:
        from deepagents_app.services.redis_conn import get_shared_redis

        ttl = max(1, int(ttl_s))
        token = f"{_WORKER_ID}:{uuid.uuid4().hex}"
        try:
            client = await get_shared_redis()
            acquired = await client.set(
                key, token, nx=True, ex=ttl
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s 获取全局锁失败，跳过本轮：%s", key, exc)
            return
        if not acquired:
            logger.debug("%s 本轮已由其他 worker 执行", key)
            return
        renew_task = asyncio.create_task(
            _renew_lease(client, key=key, token=token, ttl_s=ttl),
            name=f"{key}-lease-renewal",
        )
        try:
            await tick()
        finally:
            renew_task.cancel()
            with suppress(asyncio.CancelledError):
                await renew_task

    return _guarded


@dataclass
class PeriodicTask:
    """一个可启停的周期任务句柄。"""

    name: str
    _stop: asyncio.Event | None = None
    _task: asyncio.Task[None] | None = None

    async def _loop(self, interval_s: float, tick: Tick) -> None:
        assert self._stop is not None
        while True:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_s)
                return
            except asyncio.TimeoutError:
                pass
            try:
                await tick()
            except Exception:  # noqa: BLE001
                logger.exception("%s 后台执行失败", self.name)

    def start(self, interval_s: float, tick: Tick) -> bool:
        """
        启动周期任务。

        Returns:
            True 表示本次新建了任务；False 表示已在跑或 interval 无效。
        """
        if interval_s <= 0:
            logger.info("%s 后台未启动（interval_s=%s）", self.name, interval_s)
            return False
        if self._task is not None and not self._task.done():
            return False
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(
            self._loop(interval_s, tick),
            name=self.name,
        )
        logger.info("%s 后台已启动 interval_s=%s", self.name, interval_s)
        return True

    async def stop(self, *, join_timeout: float = 5.0) -> None:
        """请求退出并等待结束。"""
        if self._stop is not None:
            self._stop.set()
        task = self._task
        self._task = None
        self._stop = None
        if task is None:
            return
        try:
            await asyncio.wait_for(task, timeout=join_timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
