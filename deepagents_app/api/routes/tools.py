"""
Tool 注册 API
=============

- 列表 / 详情：builtin + mcp
- 新建 / 删除：仅 MCP
- 内置工具不可改执行体、不可删（可改 status / requires_hitl）
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    offset_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import ToolCreate, ToolOut, ToolUpdate
from deepagents_app.db.session import get_async_db
from deepagents_app.services import tools as tools_svc

router = APIRouter(tags=["tools"])


@router.get("/tool/list", response_model=list[ToolOut])
async def list_tools(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    tool_type: str | None = Query(None, description="builtin | mcp"),
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
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
        offset=offset,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.post("/tool", response_model=ToolOut)
async def create_tool(
    body: ToolCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """仅创建 MCP 工具（body.mcp 为连接配置；schema 层已禁止 class_path 式创建）。"""
    return await tools_svc.create_mcp_tool(
        db,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        mcp_config=body.mcp.model_dump(),
        requires_hitl=body.requires_hitl,
        status=body.status,
        tool_id=body.id,
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
