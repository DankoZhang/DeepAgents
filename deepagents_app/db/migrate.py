#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   migrate.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   migrate.py

数据库迁移入口
==============

部署或本地建库时单独执行，不随 API 进程启动::

    python -m deepagents_app.db.migrate

等价于 ``alembic upgrade head``（URL 来自应用 Settings / DATABASE_URL）。
"""

from __future__ import annotations

import logging
import sys

from deepagents_app.config import get_settings
from deepagents_app.db.session import migrate_db


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if argv in (["-h"], ["--help"]):
        print(__doc__)
        return 0

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    settings = get_settings()
    # 只打印 host/db，避免把账号密码打到 stdout
    logging.getLogger(__name__).info(
        "迁移目标 db=%s", settings.database_url.split("@")[-1]
    )
    migrate_db()
    logging.getLogger(__name__).info("迁移完成（alembic upgrade head）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
