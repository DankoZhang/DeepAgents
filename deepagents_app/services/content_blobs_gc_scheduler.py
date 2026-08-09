"""
API 进程内 content_blob GC 后台调度
=================================

按间隔调用 ``gc_orphan_content_blobs``；间隔为 0 时不启动。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from deepagents_app.services.periodic import PeriodicTask

if TYPE_CHECKING:
    from deepagents_app.config import Settings

logger = logging.getLogger(__name__)

_runner = PeriodicTask(name="content-blob-gc")


async def _tick() -> None:
    from deepagents_app.db.session import get_async_session_factory
    from deepagents_app.services.content_blobs import gc_orphan_content_blobs

    db = get_async_session_factory()()
    try:
        deleted = await gc_orphan_content_blobs(db)
        await db.commit()
        if deleted:
            logger.info("content_blob GC 后台删除 %s 行", deleted)
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


def start_content_blob_gc_scheduler(settings: Settings) -> None:
    """若 ``content_blob_gc_interval_hours > 0``，启动后台 Task。"""
    interval_h = float(settings.content_blob_gc_interval_hours)
    if interval_h <= 0:
        logger.info(
            "content_blob GC 后台未启动（interval_hours=%s）",
            settings.content_blob_gc_interval_hours,
        )
        return
    _runner.start(interval_h * 3600, _tick)


async def stop_content_blob_gc_scheduler() -> None:
    """请求后台 Task 退出并等待结束。"""
    await _runner.stop()
