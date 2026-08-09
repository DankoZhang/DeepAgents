"""
Skills 物化目录 GC 入口
======================

内容寻址物化只写不删，需定期清掉长期未复用的目录::

    python -m deepagents_app.services.skills_gc
    python -m deepagents_app.services.skills_gc --max-age-days 7

也可由 API lifespan 按 ``SKILLS_GC_INTERVAL_HOURS`` 在后台线程周期执行。
"""

from __future__ import annotations

import argparse
import logging
import sys

from deepagents_app.config import get_settings
from deepagents_app.services.skills import gc_materialized_skills


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理过期的 Skills 物化目录")
    parser.add_argument(
        "--max-age-days",
        type=float,
        default=None,
        help="``.complete`` 超过该天数未刷新则删除（默认读配置）",
    )
    parser.add_argument(
        "--tmp-max-age-hours",
        type=float,
        default=None,
        help="残留临时目录超过该小时数则删除（默认读配置）",
    )
    args = parser.parse_args(argv if argv is not None else None)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = get_settings()
    stats = gc_materialized_skills(
        settings,
        max_age_days=args.max_age_days,
        tmp_max_age_hours=args.tmp_max_age_hours,
    )
    logging.getLogger(__name__).info("GC 结果：%s", stats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
