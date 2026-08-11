"""
Middleware 注册管理
==================

中间件目录只读对外暴露；``create_middleware`` 主要供种子/内部写入。
组装 Agent 时按快照里的 class_path + config 动态加载实例。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.db.models import MiddlewareDefinition
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.services.catalog.crud_helpers import (
    ensure_unique_owned_name,
    get_owned,
    resolve_resource_id,
)


async def list_middlewares(
    db: AsyncSession,
    *,
    owner_user_id: str,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[MiddlewareDefinition], int, str | None]:
    """列出当前用户已注册的中间件。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(MiddlewareDefinition)
        .where(MiddlewareDefinition.owner_user_id == owner_user_id)
        .order_by(MiddlewareDefinition.name, MiddlewareDefinition.id)
    )
    return await page_rows(
        db,
        stmt,
        limit=limit,
        cursor=cursor,
        sort_column=MiddlewareDefinition.name,
        id_column=MiddlewareDefinition.id,
        sort_attr="name",
    )


async def get_middleware(
    db: AsyncSession, middleware_id: str, *, owner_user_id: str
) -> MiddlewareDefinition | None:
    """按主键取中间件；不属于当前用户则视为不存在。"""

    return await get_owned(
        db, MiddlewareDefinition, middleware_id, owner_user_id=owner_user_id
    )


async def create_middleware(
    db: AsyncSession,
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

    await ensure_unique_owned_name(
        db,
        MiddlewareDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="中间件",
        message=f"中间件名已存在：{name}",
    )
    row = MiddlewareDefinition(
        id=resolve_resource_id(middleware_id, prefix="mw_", label="middleware id"),
        owner_user_id=owner_user_id,
        name=name,
        class_path=class_path,
        config=config or {},
    )
    db.add(row)
    await db.flush()
    return row
