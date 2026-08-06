"""Tool 注册管理：内置只读；API 仅可新增/编辑 MCP。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.models import AgentTool, ToolDefinition
from deepagents_app.services.revisions import (
    bump_methodologies_using_tool,
    schedule_cache_invalidation,
)


def list_tools(
    db: Session,
    *,
    owner_user_id: str,
    status: str | None = None,
    tool_type: str | None = None,
) -> list[ToolDefinition]:
    q = (
        db.query(ToolDefinition)
        .filter(ToolDefinition.owner_user_id == owner_user_id)
        .order_by(ToolDefinition.tool_type, ToolDefinition.name)
    )
    if status:
        q = q.filter(ToolDefinition.status == status)
    if tool_type:
        q = q.filter(ToolDefinition.tool_type == tool_type)
    return q.all()


def get_tool(
    db: Session, tool_id: str, *, owner_user_id: str
) -> ToolDefinition | None:
    row = db.get(ToolDefinition, tool_id)
    if row is None or row.owner_user_id != owner_user_id:
        return None
    return row


def create_builtin_tool(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    class_path: str,
    description: str = "",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """种子/内部用：写入 builtin 工具（不走对外「仅 MCP」创建 API）。"""
    if (
        db.query(ToolDefinition)
        .filter(
            ToolDefinition.owner_user_id == owner_user_id,
            ToolDefinition.name == name,
        )
        .one_or_none()
    ):
        raise BusinessError(f"工具名已存在：{name}")
    row = ToolDefinition(
        id=tool_id or f"tool_{uuid.uuid4().hex[:12]}",
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        tool_type="builtin",
        class_path=class_path,
        input_schema=input_schema,
        output_schema=output_schema,
        config=config or {},
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def create_mcp_tool(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    mcp_config: dict[str, Any],
    description: str = "",
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """前端/API：仅创建 MCP 工具。"""
    if (
        db.query(ToolDefinition)
        .filter(
            ToolDefinition.owner_user_id == owner_user_id,
            ToolDefinition.name == name,
        )
        .one_or_none()
    ):
        raise BusinessError(f"工具名已存在：{name}")
    row = ToolDefinition(
        id=tool_id or f"tool_{uuid.uuid4().hex[:12]}",
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        tool_type="mcp",
        class_path=None,
        config=dict(mcp_config),
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def update_tool(
    db: Session,
    tool_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
    mcp_config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> ToolDefinition:
    row = get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        if name is not None or description is not None or mcp_config is not None:
            raise BusinessError(
                "内置工具不可修改名称/描述/连接配置，仅可更新 status"
            )
    if name is not None:
        if name != row.name:
            clash = (
                db.query(ToolDefinition)
                .filter(
                    ToolDefinition.owner_user_id == owner_user_id,
                    ToolDefinition.name == name,
                    ToolDefinition.id != tool_id,
                )
                .one_or_none()
            )
            if clash is not None:
                raise BusinessError(f"工具名已存在：{name}")
        row.name = name
    if description is not None:
        row.description = description
    if mcp_config is not None:
        if row.tool_type != "mcp":
            raise BusinessError("仅 MCP 工具可更新连接配置")
        row.config = dict(mcp_config)
    if status is not None:
        row.status = status
    db.flush()
    if bump_related:
        bump_methodologies_using_tool(db, tool_id)
    else:
        schedule_cache_invalidation(db, all_keys=True)
    return row


def delete_tool(
    db: Session,
    tool_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    row = get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        raise BusinessError("内置工具不可删除，请改为 disabled")
    agent_ids = [
        r.agent_id
        for r in db.query(AgentTool).filter(AgentTool.tool_id == tool_id).all()
    ]
    db.delete(row)
    db.flush()
    if bump_related and agent_ids:
        from deepagents_app.services.revisions import bump_methodologies_for_agent_ids

        bump_methodologies_for_agent_ids(db, agent_ids)
    else:
        schedule_cache_invalidation(db, all_keys=True)
