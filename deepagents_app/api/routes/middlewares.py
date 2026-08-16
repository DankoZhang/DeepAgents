#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   middlewares.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   middlewares.py

Middleware API（只读）
=====================

内置中间件由用户 bootstrap 写入；前端仅可列表勾选，不可新建/编辑/删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import MiddlewareOut
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import middlewares as mw_svc
router = APIRouter(tags=["middlewares"])


@router.get("/middleware/list", response_model=list[MiddlewareOut])
async def list_middlewares(
    response: Response,
    limit: int = Depends(limit_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await mw_svc.list_middlewares(
        db,
        owner_user_id=user_id,
        limit=limit,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.get("/middleware/{middleware_id}", response_model=MiddlewareOut)
async def get_middleware(
    middleware_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await mw_svc.get_middleware(db, middleware_id, owner_user_id=user_id)
    return require_entity(row, "中间件不存在")
