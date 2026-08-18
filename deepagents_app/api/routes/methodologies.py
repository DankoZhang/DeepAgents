#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   methodologies.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   methodologies.py

方法论 API
==========

CRUD + 发布 + 勾选全局 Agent + 版本列表。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import (
    MethodologyBindAgents,
    MethodologyCreate,
    MethodologyDetailOut,
    MethodologyOut,
    MethodologyUpdate,
)
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import methodology as methodology_svc


router = APIRouter(tags=["methodology"])


@router.post("/methodology", response_model=MethodologyDetailOut)
async def create_methodology(
    body: MethodologyCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """创建草稿方法论，可选 ``agent_ids`` 立即勾选全局 Agent。"""
    return await methodology_svc.create_methodology(
        db,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        methodology_id=body.id,
        agent_ids=body.agent_ids or None,
    )


@router.get("/methodology/list", response_model=list[MethodologyOut])
async def list_methodologies(
    response: Response,
    status: str | None = Query(None, description="draft | published | archived"),
    limit: int = Depends(limit_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await methodology_svc.list_methodologies(
        db,
        owner_user_id=user_id,
        status=status,
        limit=limit,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.get("/methodology/{methodology_id}", response_model=MethodologyDetailOut)
async def get_methodology(
    methodology_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await methodology_svc.get_methodology(
        db, methodology_id, owner_user_id=user_id
    )
    return require_entity(row, "方法论不存在")


@router.patch("/methodology/{methodology_id}", response_model=MethodologyOut)
async def update_methodology(
    methodology_id: str,
    body: MethodologyUpdate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await methodology_svc.update_methodology(
        db,
        methodology_id,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
    )


@router.delete("/methodology/{methodology_id}")
async def delete_methodology(
    methodology_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    await methodology_svc.delete_methodology(
        db, methodology_id, owner_user_id=user_id
    )
    return {"ok": True}


@router.post(
    "/methodology/{methodology_id}/agents",
    response_model=MethodologyDetailOut,
)
async def bind_agents(
    methodology_id: str,
    body: MethodologyBindAgents,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """勾选全局 Agent；``replace=True`` 时先清空再绑定。"""
    return await methodology_svc.bind_methodology_agents(
        db,
        methodology_id,
        body.agent_ids,
        owner_user_id=user_id,
        replace=body.replace,
    )


@router.post("/methodology/{methodology_id}/publish", response_model=MethodologyOut)
async def publish_methodology(
    methodology_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await methodology_svc.publish_methodology(
        db, methodology_id, owner_user_id=user_id
    )


@router.post("/methodology/{methodology_id}/unpublish", response_model=MethodologyOut)
async def unpublish_methodology(
    methodology_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """
    将方法论退回 draft。

    管理 / 测试接口；产品主路径请用 ``POST /agent/{id}/disable``。
    """
    return await methodology_svc.unpublish_methodology(
        db, methodology_id, owner_user_id=user_id
    )


@router.get("/methodology/{methodology_id}/versions")
async def list_versions(
    methodology_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await methodology_svc.get_methodology_versions(
        db, methodology_id, owner_user_id=user_id
    )
