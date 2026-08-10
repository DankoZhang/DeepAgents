"""
运行时 GC（Skills 物化目录 + 孤儿 content_blob）
=============================================

统一入口：

- 后台：API lifespan 调用 ``start_gc_scheduler`` / ``stop_gc_scheduler``
  （单 PeriodicTask；Skills / blob 各自 Redis 单飞锁，间隔仍读原配置）
- CLI::

    python -m deepagents_app.services.infra.gc
    python -m deepagents_app.services.infra.gc skills --max-age-days 7
    python -m deepagents_app.services.infra.gc blobs
    python -m deepagents_app.services.infra.gc all

领域清理逻辑仍在 ``catalog.skills.gc_materialized_skills`` /
``versioning.content_blobs.gc_orphan_content_blobs``。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import TYPE_CHECKING, Any

from deepagents_app.services.infra.periodic import PeriodicTask, single_flight

if TYPE_CHECKING:
    from deepagents_app.config import Settings

logger = logging.getLogger(__name__)

_runner = PeriodicTask(name="gc")
_settings: Settings | None = None

_LOCK_SKILLS = "deepagents:gc:skills"
_LOCK_BLOBS = "deepagents:gc:content_blob"


# ── 单次执行 ──────────────────────────────────────────────────────────


def run_skills_gc(
    settings: Settings,
    *,
    max_age_days: float | None = None,
    tmp_max_age_hours: float | None = None,
) -> dict[str, int]:
    """清理过期 Skills 物化目录（同步，可在线程池调用）。"""
    from deepagents_app.services.catalog.skills import gc_materialized_skills

    return gc_materialized_skills(
        settings,
        max_age_days=max_age_days,
        tmp_max_age_hours=tmp_max_age_hours,
    )


async def run_content_blob_gc(*, migrate: bool = False) -> int:
    """
    清理孤儿 content_blob。

    ``migrate=True`` 供 CLI 使用（确保 schema 最新）；后台调度不迁库。
    """
    from deepagents_app.config import get_settings
    from deepagents_app.db.session import (
        get_async_session_factory,
        migrate_db,
        reset_engine,
    )
    from deepagents_app.services.versioning.content_blobs import gc_orphan_content_blobs

    settings = get_settings()
    if migrate:
        migrate_db(settings.database_url)

    db = get_async_session_factory()()
    try:
        deleted = await gc_orphan_content_blobs(db)
        await db.commit()
        return deleted
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
        if migrate:
            reset_engine()


# ── 后台调度（一个 PeriodicTask）──────────────────────────────────────


async def _tick_skills() -> None:
    assert _settings is not None
    stats = await asyncio.to_thread(run_skills_gc, _settings)
    if any(stats.values()):
        logger.info("Skills GC 后台结果：%s", stats)


async def _tick_blobs() -> None:
    deleted = await run_content_blob_gc(migrate=False)
    if deleted:
        logger.info("content_blob GC 后台删除 %s 行", deleted)


def _skills_enabled(settings: Settings) -> bool:
    return (
        float(settings.skills_gc_interval_hours) > 0
        and float(settings.skills_gc_max_age_days) > 0
    )


def _blobs_enabled(settings: Settings) -> bool:
    return float(settings.content_blob_gc_interval_hours) > 0


def start_gc_scheduler(settings: Settings) -> None:
    """
    启动统一 GC 后台任务。

    Skills / content_blob 仍用各自间隔配置与 Redis 锁；循环间隔取两者中
    较小的正值。某项间隔为 0（或 Skills 的 max_age 为 0）则不跑该项。
    """
    global _settings
    skills_on = _skills_enabled(settings)
    blobs_on = _blobs_enabled(settings)
    if not skills_on and not blobs_on:
        logger.info(
            "GC 后台未启动（skills_interval=%s max_age_days=%s "
            "content_blob_interval=%s）",
            settings.skills_gc_interval_hours,
            settings.skills_gc_max_age_days,
            settings.content_blob_gc_interval_hours,
        )
        return

    _settings = settings
    skills_s = float(settings.skills_gc_interval_hours) * 3600
    blobs_s = float(settings.content_blob_gc_interval_hours) * 3600

    ticks: list[Any] = []
    intervals: list[float] = []
    if skills_on:
        intervals.append(skills_s)
        ticks.append(single_flight(_LOCK_SKILLS, skills_s * 0.9, _tick_skills))
    if blobs_on:
        intervals.append(blobs_s)
        ticks.append(single_flight(_LOCK_BLOBS, blobs_s * 0.9, _tick_blobs))

    async def _tick_all() -> None:
        for guarded in ticks:
            await guarded()

    _runner.start(min(intervals), _tick_all)


async def stop_gc_scheduler() -> None:
    """请求后台 Task 退出并等待结束。"""
    global _settings
    await _runner.stop()
    _settings = None


# ── CLI ───────────────────────────────────────────────────────────────


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DeepAgents 运行时 GC（Skills 物化目录 / 孤儿 content_blob）"
    )
    sub = parser.add_subparsers(dest="target")

    skills_p = sub.add_parser("skills", help="清理过期 Skills 物化目录")
    skills_p.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        help="``.complete`` 超过该天数未刷新则删除（默认读配置）",
    )
    skills_p.add_argument(
        "--tmp-max-age-hours",
        type=float,
        default=None,
        help="残留临时目录超过该小时数则删除（默认读配置）",
    )

    sub.add_parser("blobs", help="清理孤儿 content_blob")

    all_p = sub.add_parser("all", help="依次执行 skills + blobs（默认）")
    all_p.add_argument("--max-age-days", type=float, default=None)
    all_p.add_argument("--tmp-max-age-hours", type=float, default=None)

    args = parser.parse_args(argv if argv is not None else None)
    target = args.target or "all"
    _configure_logging()

    from deepagents_app.config import get_settings

    settings = get_settings()
    log = logging.getLogger(__name__)

    if target in ("skills", "all"):
        stats = run_skills_gc(
            settings,
            max_age_days=getattr(args, "max_age_days", None),
            tmp_max_age_hours=getattr(args, "tmp_max_age_hours", None),
        )
        log.info("Skills GC 结果：%s", stats)

    if target in ("blobs", "all"):
        deleted = asyncio.run(run_content_blob_gc(migrate=True))
        log.info("content_blob GC 删除 %s 行", deleted)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
