"""
Middleware 注册管理
==================

对外 API 只读；``create_middleware`` 供种子与内部使用。
"""

# 推迟注解求值
from __future__ import annotations

# 生成中间件主键
import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError
from deepagents_app.db.models import MiddlewareDefinition


def list_middlewares(
    db: Session, *, owner_user_id: str
) -> list[MiddlewareDefinition]:
    return (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.owner_user_id == owner_user_id)
        .order_by(MiddlewareDefinition.name)
        .all()
    )


def get_middleware(
    db: Session, middleware_id: str, *, owner_user_id: str
) -> MiddlewareDefinition | None:
    row = db.get(MiddlewareDefinition, middleware_id)
    if row is None or row.owner_user_id != owner_user_id:
        return None
    return row


def create_middleware(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    class_path: str,
    config: dict[str, Any] | None = None,
    middleware_id: str | None = None,
) -> MiddlewareDefinition:
    if (
        db.query(MiddlewareDefinition)
        .filter(
            MiddlewareDefinition.owner_user_id == owner_user_id,
            MiddlewareDefinition.name == name,
        )
        .one_or_none()
    ):
        raise BusinessError(f"中间件名已存在：{name}")
    row = MiddlewareDefinition(
        id=middleware_id or f"mw_{uuid.uuid4().hex[:12]}",
        owner_user_id=owner_user_id,
        name=name,
        class_path=class_path,
        config=config or {},
    )
    db.add(row)
    db.flush()
    return row
