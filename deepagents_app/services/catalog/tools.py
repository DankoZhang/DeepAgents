#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   tools.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   tools.py

Tool 注册管理
=============

- 内置工具（builtin）：种子写入；API 可改 status / requires_hitl
- MCP / HTTP 工具：前端/API 可创建与编辑执行配置及 HITL
- 变更后默认 bump 引用该方法论，保证旧会话快照可重建
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.models import AgentTool, ToolDefinition
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.services.catalog.crud_helpers import (
    ensure_unique_owned_name,
    get_owned,
    resolve_resource_id,
)
from deepagents_app.services.versioning.revisions import (
    propagate_methodology_change_for_agent_ids,
    propagate_methodology_change_using_resource,
)
from deepagents_app.utils.http_tool_safety import validate_http_tool_config
from deepagents_app.utils.mcp_safety import validate_mcp_config


def _invalidate_mcp_cache(tool_id: str) -> None:
    from deepagents_app.registries.tools import invalidate_mcp_tools_cache

    invalidate_mcp_tools_cache(tool_id=tool_id)


async def list_tools(
    db: AsyncSession,
    *,
    owner_user_id: str,
    status: str | None = None,
    tool_type: str | None = None,
    limit: int = DEFAULT_LIMIT,
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
    """种子/内部用：写入 builtin 工具（不走对外创建 API）。"""

    await ensure_unique_owned_name(
        db,
        ToolDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="工具",
        message=f"工具名已存在：{name}",
    )
    row = ToolDefinition(
        id=resolve_resource_id(tool_id, prefix="tool_", label="tool id"),
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


async def _create_registered_tool(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    tool_type: str,
    config: dict[str, Any],
    description: str,
    requires_hitl: bool,
    status: str,
    tool_id: str | None,
) -> ToolDefinition:
    await ensure_unique_owned_name(
        db,
        ToolDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="工具",
        message=f"工具名已存在：{name}",
    )
    row = ToolDefinition(
        id=resolve_resource_id(tool_id, prefix="tool_", label="tool id"),
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        tool_type=tool_type,
        class_path=None,
        requires_hitl=bool(requires_hitl),
        config=config,
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
    """前端/API：创建 MCP 工具（连接信息放在 config）。"""
    return await _create_registered_tool(
        db,
        owner_user_id=owner_user_id,
        name=name,
        tool_type="mcp",
        config=validate_mcp_config(mcp_config),
        description=description,
        requires_hitl=requires_hitl,
        status=status,
        tool_id=tool_id,
    )


async def create_http_tool(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    http_config: dict[str, Any],
    description: str = "",
    requires_hitl: bool = False,
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """前端/API：创建 HTTP 工具（接口信息放在 config）。"""
    return await _create_registered_tool(
        db,
        owner_user_id=owner_user_id,
        name=name,
        tool_type="http",
        config=validate_http_tool_config(http_config),
        description=description,
        requires_hitl=requires_hitl,
        status=status,
        tool_id=tool_id,
    )


async def update_tool(
    db: AsyncSession,
    tool_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    http_config: dict[str, Any] | None = None,
    requires_hitl: bool | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> ToolDefinition:
    """
    更新工具。

    内置工具仅允许改 status / requires_hitl；MCP / HTTP 可改名称/描述/执行配置/HITL。
    不允许更改 tool_type。``bump_related=True`` 时升版所有引用该方法论。
    """
    row = await get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        if (
            name is not None
            or description is not None
            or mcp_config is not None
            or http_config is not None
        ):
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
    if http_config is not None:
        if row.tool_type != "http":
            raise BusinessError("仅 HTTP 工具可更新接口配置")
        row.config = validate_http_tool_config(http_config)
    if requires_hitl is not None:
        row.requires_hitl = bool(requires_hitl)
    if status is not None:
        row.status = status
    await db.flush()
    if row.tool_type == "mcp":
        _invalidate_mcp_cache(tool_id)
    await propagate_methodology_change_using_resource(
        db,
        kind="tool",
        resource_id=tool_id,
        bump_related=bump_related,
    )
    return row


async def delete_tool(
    db: AsyncSession,
    tool_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """删除 MCP / HTTP 工具；内置工具禁止删除（应改为 disabled）。"""
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
    if row.tool_type == "mcp":
        _invalidate_mcp_cache(tool_id)
    await propagate_methodology_change_for_agent_ids(
        db, agent_ids, bump_related=bump_related
    )


async def test_tool_connectivity(
    *,
    tool_type: str,
    mcp_config: dict[str, Any] | None = None,
    http_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """按内联配置试连（不写库）。"""
    if tool_type == "mcp":
        if not mcp_config:
            raise BusinessError("mcp 工具必须提供 mcp")
        from deepagents_app.registries.tools import probe_mcp_connection

        return await probe_mcp_connection(mcp_config)
    if tool_type == "http":
        if not http_config:
            raise BusinessError("http 工具必须提供 http")
        from deepagents_app.registries.http_tools import probe_http_connection

        return await probe_http_connection(http_config)
    raise BusinessError(f"不支持的 tool_type：{tool_type}")


async def test_tool_by_id(
    db: AsyncSession, tool_id: str, *, owner_user_id: str
) -> dict[str, Any]:
    """按目录 id 测连通性。"""
    row = await get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.status != "active":
        return {
            "ok": False,
            "message": f"工具状态为 {row.status}，未测试",
            "detail": None,
        }
    if row.tool_type == "builtin":
        return {
            "ok": False,
            "message": "内置工具无需连通性测试",
            "detail": None,
        }
    return await test_tool_connectivity(
        tool_type=row.tool_type,
        mcp_config=dict(row.config or {}) if row.tool_type == "mcp" else None,
        http_config=dict(row.config or {}) if row.tool_type == "http" else None,
    )
