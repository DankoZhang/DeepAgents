"""Middleware 注册管理。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import MiddlewareDefinition
from deepagents_app.services.agent_factory import invalidate_agent_cache


def list_middlewares(db: Session) -> list[MiddlewareDefinition]:
    return db.query(MiddlewareDefinition).order_by(MiddlewareDefinition.name).all()


def get_middleware(db: Session, middleware_id: str) -> MiddlewareDefinition | None:
    return db.get(MiddlewareDefinition, middleware_id)


def create_middleware(
    db: Session,
    *,
    name: str,
    class_path: str,
    config: dict[str, Any] | None = None,
    middleware_id: str | None = None,
) -> MiddlewareDefinition:
    if (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.name == name)
        .one_or_none()
    ):
        raise ValueError(f"中间件名已存在：{name}")
    row = MiddlewareDefinition(
        id=middleware_id or f"mw_{uuid.uuid4().hex[:12]}",
        name=name,
        class_path=class_path,
        config=config or {},
    )
    db.add(row)
    db.flush()
    return row


def update_middleware(
    db: Session,
    middleware_id: str,
    *,
    name: str | None = None,
    class_path: str | None = None,
    config: dict[str, Any] | None = None,
) -> MiddlewareDefinition:
    row = db.get(MiddlewareDefinition, middleware_id)
    if row is None:
        raise LookupError(f"中间件不存在：{middleware_id}")
    if name is not None:
        row.name = name
    if class_path is not None:
        row.class_path = class_path
    if config is not None:
        row.config = config
    invalidate_agent_cache()
    db.flush()
    return row


def delete_middleware(db: Session, middleware_id: str) -> None:
    row = db.get(MiddlewareDefinition, middleware_id)
    if row is None:
        raise LookupError(f"中间件不存在：{middleware_id}")
    invalidate_agent_cache()
    db.delete(row)
    db.flush()
