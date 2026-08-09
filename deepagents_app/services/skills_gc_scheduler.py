"""
API 进程内 Skills GC 后台调度
============================

按间隔在线程池调用 ``gc_materialized_skills``；间隔为 0 时不启动。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from deepagents_app.services.periodic import PeriodicTask

if TYPE_CHECKING:
    from deepagents_app.config import Settings

logger = logging.getLogger(__name__)

_runner = PeriodicTask(name="skills-gc")
_settings: Settings | None = None


async def _tick() -> None:
    assert _settings is not None
    from deepagents_app.services.skills import gc_materialized_skills

    await asyncio.to_thread(gc_materialized_skills, _settings)


def start_skills_gc_scheduler(settings: Settings) -> None:
    """若 ``skills_gc_interval_hours > 0`` 且 ``skills_gc_max_age_days > 0``，启动后台 Task。"""
    global _settings
    interval_h = float(settings.skills_gc_interval_hours)
    if interval_h <= 0 or float(settings.skills_gc_max_age_days) <= 0:
        logger.info(
            "Skills GC 后台未启动（interval_hours=%s max_age_days=%s）",
            settings.skills_gc_interval_hours,
            settings.skills_gc_max_age_days,
        )
        return
    _settings = settings
    _runner.start(interval_h * 3600, _tick)


async def stop_skills_gc_scheduler() -> None:
    """请求后台 Task 退出并等待结束。"""
    global _settings
    await _runner.stop()
    _settings = None
