"""
Tool 注册管理
=============

- 内置工具（builtin）：种子写入；API 可改 status / requires_hitl
- MCP 工具：前端/API 可创建与编辑连接配置及 HITL
- 变更后默认 bump 引用该方法论，保证旧会话快照可重建
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.models import AgentTool, ToolDefinition
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.ownership import validate_resource_id
from deepagents_app.services.crud_helpers import ensure_unique_owned_name, get_owned
from deepagents_app.services.revisions import (
    bump_methodologies_for_agent_ids,
    bump_methodologies_using_tool,
    schedule_cache_invalidation_for_agent_ids,
)
from deepagents_app.utils.mcp_safety import validate_mcp_config


def _invalidate_mcp_cache(tool_id: str) -> None:
    from deepagents_app.registries.tools import clear_mcp_tools_cache

    clear_mcp_tools_cache(tool_id=tool_id)


async def list_tools(
    db: AsyncSession,
    *,
    owner_user_id: str,
    status: str | None = None,
    tool_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    cursor: str | None = None,
) -> tuple[list[ToolDefinition], int, str | None]:
    """列出当前用户的工具目录；可按 status / tool_type 过滤。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(ToolDefinition)
        .where(ToolDefinition.owner_user_id == owner_user_id)
        .order_by(ToolDefinition.name, ToolDefinition.id)
    )
    if status:
        stmt = stmt.where(ToolDefinition.status == status)
    if tool_type:
        stmt = stmt.where(ToolDefinition.tool_type == tool_type)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        offset=offset,
        cursor=cursor,
        sort_column=ToolDefinition.name,
        id_column=ToolDefinition.id,
        sort_attr="name",
    )


async def get_tool(
    db: AsyncSession, tool_id: str, *, owner_user_id: str
) -> ToolDefinition | None:
    """按主键取工具；不属于当前用户则视为不存在。"""

    return await get_owned(db, ToolDefinition, tool_id, owner_user_id=owner_user_id)


async def create_builtin_tool(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    class_path: str,
    description: str = "",
    requires_hitl: bool = False,
    config: dict[str, Any] | None = None,
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """种子/内部用：写入 builtin 工具（不走对外「仅 MCP」创建 API）。"""

    await ensure_unique_owned_name(
        db,
        ToolDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="工具",
        message=f"工具名已存在：{name}",
    )
    row = ToolDefinition(
        id=_resolve_tool_id(tool_id),
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        tool_type="builtin",
        class_path=class_path,
        requires_hitl=bool(requires_hitl),
        config=config or {},
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def create_mcp_tool(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    mcp_config: dict[str, Any],
    description: str = "",
    requires_hitl: bool = True,
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """前端/API：仅创建 MCP 工具（连接信息放在 config）。"""
    safe_config = validate_mcp_config(mcp_config)

    await ensure_unique_owned_name(
        db,
        ToolDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="工具",
        message=f"工具名已存在：{name}",
    )
    row = ToolDefinition(
        id=_resolve_tool_id(tool_id),
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        tool_type="mcp",
        class_path=None,
        requires_hitl=bool(requires_hitl),
        config=safe_config,
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def update_tool(
    db: AsyncSession,
    tool_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    requires_hitl: bool | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> ToolDefinition:
    """
    更新工具。

    内置工具仅允许改 status / requires_hitl；MCP 可改名称/描述/连接配置/HITL。
    ``bump_related=True`` 时升版所有引用该方法论。
    """
    row = await get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        if name is not None or description is not None or mcp_config is not None:
            raise BusinessError(
                "内置工具不可修改名称/描述/连接配置，仅可更新 status / requires_hitl"
            )
    if name is not None:
        if name != row.name:

            await ensure_unique_owned_name(
                db,
                ToolDefinition,
                owner_user_id=owner_user_id,
                name=name,
                exclude_id=tool_id,
                label="工具",
                message=f"工具名已存在：{name}",
            )
        row.name = name
    if description is not None:
        row.description = description
    if mcp_config is not None:
        if row.tool_type != "mcp":
            raise BusinessError("仅 MCP 工具可更新连接配置")
        row.config = validate_mcp_config(mcp_config)
    if requires_hitl is not None:
        row.requires_hitl = bool(requires_hitl)
    if status is not None:
        row.status = status
    await db.flush()
    if row.tool_type == "mcp":
        _invalidate_mcp_cache(tool_id)
    if bump_related:
        await bump_methodologies_using_tool(db, tool_id)
    else:
        agent_ids = [
            r.agent_id
            for r in await db.scalars(
                select(AgentTool).where(AgentTool.tool_id == tool_id)
            )
        ]
        await schedule_cache_invalidation_for_agent_ids(db, agent_ids)
    return row


async def delete_tool(
    db: AsyncSession,
    tool_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """删除 MCP 工具；内置工具禁止删除（应改为 disabled）。"""
    row = await get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        raise BusinessError("内置工具不可删除，请改为 disabled")
    agent_ids = [
        r.agent_id
        for r in await db.scalars(
            select(AgentTool).where(AgentTool.tool_id == tool_id)
        )
    ]
    await db.delete(row)
    await db.flush()
    _invalidate_mcp_cache(tool_id)
    if bump_related:
        if agent_ids:

            await bump_methodologies_for_agent_ids(db, agent_ids)
    elif agent_ids:
        await schedule_cache_invalidation_for_agent_ids(db, agent_ids)


def _resolve_tool_id(tool_id: str | None) -> str:
    resolved = tool_id or f"tool_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="tool id")
