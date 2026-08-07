"""
Tool 注册管理
=============

- 内置工具（builtin）：种子写入，API 侧基本只读（仅可改 status）
- MCP 工具：前端/API 可创建与编辑连接配置
- 变更后默认 bump 引用该方法论，保证旧会话快照可重建
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.models import AgentTool, ToolDefinition
from deepagents_app.ownership import validate_resource_id
from deepagents_app.services.revisions import (
    bump_methodologies_using_tool,
    schedule_cache_invalidation_for_agent_ids,
)


def list_tools(
    db: Session,
    *,
    owner_user_id: str,
    status: str | None = None,
    tool_type: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[ToolDefinition], int]:
    """列出当前用户的工具目录；可按 status / tool_type 过滤。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(ToolDefinition)
        .filter(ToolDefinition.owner_user_id == owner_user_id)
        .order_by(ToolDefinition.tool_type, ToolDefinition.name)
    )
    if status:
        q = q.filter(ToolDefinition.status == status)
    if tool_type:
        q = q.filter(ToolDefinition.tool_type == tool_type)
    return paginate_query(q, limit=limit, offset=offset)


def get_tool(
    db: Session, tool_id: str, *, owner_user_id: str
) -> ToolDefinition | None:
    """按主键取工具；不属于当前用户则视为不存在。"""
    from deepagents_app.services.crud_helpers import get_owned

    return get_owned(db, ToolDefinition, tool_id, owner_user_id=owner_user_id)


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
    from deepagents_app.services.crud_helpers import ensure_unique_owned_name

    ensure_unique_owned_name(
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
    """前端/API：仅创建 MCP 工具（连接信息放在 config）。"""
    from deepagents_app.services.crud_helpers import ensure_unique_owned_name

    ensure_unique_owned_name(
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
    """
    更新工具。

    内置工具仅允许改 status；MCP 可改名称/描述/连接配置。
    ``bump_related=True`` 时升版所有引用该方法论。
    """
    row = get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    # 内置工具由代码种子管理，禁止改语义字段，避免与 class_path 脱节
    if row.tool_type == "builtin":
        if name is not None or description is not None or mcp_config is not None:
            raise BusinessError(
                "内置工具不可修改名称/描述/连接配置，仅可更新 status"
            )
    if name is not None:
        if name != row.name:
            from deepagents_app.services.crud_helpers import ensure_unique_owned_name

            ensure_unique_owned_name(
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
        row.config = dict(mcp_config)
    if status is not None:
        row.status = status
    db.flush()
    if bump_related:
        bump_methodologies_using_tool(db, tool_id)
    else:
        agent_ids = [
            r.agent_id
            for r in db.query(AgentTool).filter(AgentTool.tool_id == tool_id).all()
        ]
        schedule_cache_invalidation_for_agent_ids(db, agent_ids)
    return row


def delete_tool(
    db: Session,
    tool_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """删除 MCP 工具；内置工具禁止删除（应改为 disabled）。"""
    row = get_tool(db, tool_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"工具不存在：{tool_id}")
    if row.tool_type == "builtin":
        raise BusinessError("内置工具不可删除，请改为 disabled")
    # 先记下引用再删，删除后 AgentTool 行会级联消失
    agent_ids = [
        r.agent_id
        for r in db.query(AgentTool).filter(AgentTool.tool_id == tool_id).all()
    ]
    db.delete(row)
    db.flush()
    if bump_related:
        if agent_ids:
            from deepagents_app.services.revisions import bump_methodologies_for_agent_ids

            bump_methodologies_for_agent_ids(db, agent_ids)
    elif agent_ids:
        schedule_cache_invalidation_for_agent_ids(db, agent_ids)


def _resolve_tool_id(tool_id: str | None) -> str:
    resolved = tool_id or f"tool_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="tool id")
