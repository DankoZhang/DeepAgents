"""
周期性后台任务辅助
==================

供 Skills / content_blob 等 GC 调度器复用：按间隔跑协程，可优雅停止。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

Tick = Callable[[], Awaitable[None]]


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
