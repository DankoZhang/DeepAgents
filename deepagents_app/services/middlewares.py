"""
Middleware 注册管理
==================

中间件目录只读对外暴露；``create_middleware`` 主要供种子/内部写入。
组装 Agent 时按快照里的 class_path + config 动态加载实例。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import MiddlewareDefinition
from deepagents_app.ownership import validate_resource_id


def list_middlewares(
    db: Session,
    *,
    owner_user_id: str,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[MiddlewareDefinition], int]:
    """列出当前用户已注册的中间件。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.owner_user_id == owner_user_id)
        .order_by(MiddlewareDefinition.name)
    )
    return paginate_query(q, limit=limit, offset=offset)


def get_middleware(
    db: Session, middleware_id: str, *, owner_user_id: str
) -> MiddlewareDefinition | None:
    """按主键取中间件；不属于当前用户则视为不存在。"""
    from deepagents_app.services.crud_helpers import get_owned

    return get_owned(
        db, MiddlewareDefinition, middleware_id, owner_user_id=owner_user_id
    )


def create_middleware(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    class_path: str,
    config: dict[str, Any] | None = None,
    middleware_id: str | None = None,
) -> MiddlewareDefinition:
    """
    写入一条中间件目录项。

    ``class_path`` 指向可 import 的中间件类；``config`` 作为构造参数。
    """
    from deepagents_app.services.crud_helpers import ensure_unique_owned_name

    ensure_unique_owned_name(
        db,
        MiddlewareDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="中间件",
        message=f"中间件名已存在：{name}",
    )
    row = MiddlewareDefinition(
        id=_resolve_middleware_id(middleware_id),
        owner_user_id=owner_user_id,
        name=name,
        class_path=class_path,
        config=config or {},
    )
    db.add(row)
    db.flush()
    return row


def _resolve_middleware_id(middleware_id: str | None) -> str:
    resolved = middleware_id or f"mw_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="middleware id")
