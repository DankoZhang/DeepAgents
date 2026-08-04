"""
Middleware Registry
===================

数据库存元信息；运行时按 ``class_path`` 实例化。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import MiddlewareDefinition
from deepagents_app.registries.tools import resolve_class_path

logger = logging.getLogger(__name__)


def load_middleware_object(mw_def: MiddlewareDefinition) -> Any:
    """加载并实例化 Middleware（支持无参类或已实例化对象）。"""
    obj = resolve_class_path(mw_def.class_path)
    cfg = dict(mw_def.config or {})
    if isinstance(obj, type):
        return obj(**cfg) if cfg else obj()
    if callable(obj) and not hasattr(obj, "name"):
        return obj(**cfg) if cfg else obj()
    return obj


def load_middlewares_by_ids(db: Session, middleware_ids: list[str]) -> list[Any]:
    """按 id 列表加载中间件（保持传入顺序；缺失则跳过并告警）。"""
    if not middleware_ids:
        return []
    rows = (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.id.in_(middleware_ids))
        .all()
    )
    by_id = {r.id: r for r in rows}
    result: list[Any] = []
    for mid in middleware_ids:
        row = by_id.get(mid)
        if row is None:
            logger.warning("中间件不存在，跳过：%s", mid)
            continue
        result.append(load_middleware_object(row))
    return result
