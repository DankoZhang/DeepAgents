"""
用户 bootstrap 会话上下文
========================

CLI / lifespan 共用：migrate（可选）→ ensure_user_bootstrap → commit → 缓存失效。
请求路径请继续用 ``get_db`` + ``POST /api/bootstrap``，不要走这里。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.orm import Session

from deepagents_app.db.seed import ensure_user_bootstrap
from deepagents_app.db.session import get_session_factory, migrate_db
from deepagents_app.services.revisions import flush_cache_invalidations


@contextmanager
def bootstrapped_db_session(
    user_id: str,
    *,
    migrate: bool = False,
) -> Iterator[Session]:
    """打开 Session，幂等灌该用户种子后 yield；退出时 commit / rollback / close。"""
    if migrate:
        migrate_db()
    db = get_session_factory()()
    try:
        ensure_user_bootstrap(db, user_id)
        db.commit()
        flush_cache_invalidations(db)
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
