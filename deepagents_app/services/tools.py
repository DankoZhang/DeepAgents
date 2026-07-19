"""Tool 注册管理（元信息 CRUD，不存 Python 代码）。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import ToolDefinition
from deepagents_app.services.agent_factory import invalidate_agent_cache


def list_tools(db: Session, *, status: str | None = None) -> list[ToolDefinition]:
    q = db.query(ToolDefinition).order_by(ToolDefinition.name)
    if status:
        q = q.filter(ToolDefinition.status == status)
    return q.all()


def get_tool(db: Session, tool_id: str) -> ToolDefinition | None:
    return db.get(ToolDefinition, tool_id)


def create_tool(
    db: Session,
    *,
    name: str,
    class_path: str,
    description: str = "",
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    if db.query(ToolDefinition).filter(ToolDefinition.name == name).one_or_none():
        raise ValueError(f"工具名已存在：{name}")
    row = ToolDefinition(
        id=tool_id or f"tool_{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        class_path=class_path,
        input_schema=input_schema,
        output_schema=output_schema,
        config=config or {},
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def update_tool(
    db: Session,
    tool_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    class_path: str | None = None,
    input_schema: dict[str, Any] | None = None,
    output_schema: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
) -> ToolDefinition:
    row = db.get(ToolDefinition, tool_id)
    if row is None:
        raise LookupError(f"工具不存在：{tool_id}")
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if class_path is not None:
        row.class_path = class_path
    if input_schema is not None:
        row.input_schema = input_schema
    if output_schema is not None:
        row.output_schema = output_schema
    if config is not None:
        row.config = config
    if status is not None:
        row.status = status
    # class_path / status 变更会影响已缓存的 Compiled Agent
    invalidate_agent_cache()
    db.flush()
    return row


def delete_tool(db: Session, tool_id: str) -> None:
    row = db.get(ToolDefinition, tool_id)
    if row is None:
        raise LookupError(f"工具不存在：{tool_id}")
    invalidate_agent_cache()
    db.delete(row)
    db.flush()
