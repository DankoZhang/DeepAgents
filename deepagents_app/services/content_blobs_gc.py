"""
content_blob 孤儿 GC 入口
========================

删除未被任何方法论快照引用的正文 hash::

    python -m deepagents_app.services.content_blobs_gc
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from deepagents_app.config import get_settings
from deepagents_app.db.session import get_async_session_factory, migrate_db, reset_engine
from deepagents_app.services.content_blobs import gc_orphan_content_blobs


async def _run() -> int:
    settings = get_settings()
    migrate_db(settings.database_url)
    factory = get_async_session_factory()
    db = factory()
    try:
        deleted = await gc_orphan_content_blobs(db)
        await db.commit()
        logging.getLogger(__name__).info("content_blob GC 删除 %s 行", deleted)
        return 0
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()
        reset_engine()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理孤儿 content_blob")
    parser.parse_args(argv if argv is not None else None)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
