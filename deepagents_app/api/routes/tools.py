#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   tools.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   tools.py

Tool 注册 API
=============

- 列表 / 详情：builtin + mcp + http
- 新建 / 删除：MCP 与 HTTP（禁止 class_path）
- 内置工具不可改执行体、不可删（可改 status / requires_hitl）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import BusinessError, require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import (
    ToolCreate,
    ToolOut,
    ToolTestRequest,
    ToolTestResult,
    ToolUpdate,
)
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import tools as tools_svc
router = APIRouter(tags=["tools"])


@router.get("/tool/list", response_model=list[ToolOut])
async def list_tools(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    tool_type: str | None = Query(None, description="builtin | mcp | http"),
    limit: int = Depends(limit_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await tools_svc.list_tools(
        db,
        owner_user_id=user_id,
        status=status,
        tool_type=tool_type,
        limit=limit,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.post("/tool/test", response_model=ToolTestResult)
async def test_tool_inline(
    body: ToolTestRequest,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """
    连通性测试。

    - 传 ``tool_id``：用目录已存配置测试
    - 否则用 body 内联 mcp / http（保存前试连）
    """
    if body.tool_id:
        return await tools_svc.test_tool_by_id(
            db, body.tool_id, owner_user_id=user_id
        )
    return await tools_svc.test_tool_connectivity(
        tool_type=str(body.tool_type),
        mcp_config=body.mcp.model_dump() if body.mcp else None,
        http_config=body.http.model_dump() if body.http else None,
    )


@router.post("/tool", response_model=ToolOut)
async def create_tool(
    body: ToolCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """创建 MCP 或 HTTP 工具（schema 层已禁止 class_path 式创建）。"""
    common = {
        "owner_user_id": user_id,
        "name": body.name,
        "description": body.description,
        "status": body.status,
        "tool_id": body.id,
    }
    if body.tool_type == "http":
        if body.http is None:
            raise BusinessError("http 工具必须提供 http")
        return await tools_svc.create_http_tool(
            db,
            http_config=body.http.model_dump(),
            requires_hitl=False if body.requires_hitl is None else body.requires_hitl,
            **common,
        )
    if body.mcp is None:
        raise BusinessError("mcp 工具必须提供 mcp")
    return await tools_svc.create_mcp_tool(
        db,
        mcp_config=body.mcp.model_dump(),
        requires_hitl=True if body.requires_hitl is None else body.requires_hitl,
        **common,
    )


@router.get("/tool/{tool_id}", response_model=ToolOut)
async def get_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await tools_svc.get_tool(db, tool_id, owner_user_id=user_id)
    return require_entity(row, "工具不存在")


@router.patch("/tool/{tool_id}", response_model=ToolOut)
async def update_tool(
    tool_id: str,
    body: ToolUpdate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await tools_svc.update_tool(
        db,
        tool_id,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        mcp_config=body.mcp.model_dump() if body.mcp else None,
        http_config=body.http.model_dump() if body.http else None,
        requires_hitl=body.requires_hitl,
        status=body.status,
    )


@router.delete("/tool/{tool_id}")
async def delete_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    await tools_svc.delete_tool(db, tool_id, owner_user_id=user_id)
    return {"ok": True}


@router.post("/tool/{tool_id}/test", response_model=ToolTestResult)
async def test_tool(
    tool_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await tools_svc.test_tool_by_id(db, tool_id, owner_user_id=user_id)
