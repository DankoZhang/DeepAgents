"""Agent 配置管理：创建 / 编辑 / 绑定 Tool / Middleware。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from deepagents_app.db.models import (
    AgentDefinition,
    AgentMiddleware,
    AgentTool,
    Methodology,
    MiddlewareDefinition,
    ToolDefinition,
)
from deepagents_app.services.agent_factory import invalidate_agent_cache
from deepagents_app.services.revisions import snapshot_methodology


def list_agents(db: Session, methodology_id: str) -> list[AgentDefinition]:
    return (
        db.query(AgentDefinition)
        .options(
            joinedload(AgentDefinition.tools),
            joinedload(AgentDefinition.middlewares),
        )
        .filter(AgentDefinition.methodology_id == methodology_id)
        .order_by(AgentDefinition.name)
        .all()
    )


def get_agent(db: Session, agent_id: str) -> AgentDefinition | None:
    # 避免 identity map 中旧的 tools/middlewares 集合导致绑定后仍为空
    db.expire_all()
    return (
        db.query(AgentDefinition)
        .options(
            joinedload(AgentDefinition.tools),
            joinedload(AgentDefinition.middlewares),
        )
        .filter(AgentDefinition.id == agent_id)
        .one_or_none()
    )


def create_agent(
    db: Session,
    *,
    methodology_id: str,
    name: str,
    system_prompt: str = "",
    model: str | None = None,
    temperature: float | None = None,
    config: dict[str, Any] | None = None,
    agent_id: str | None = None,
    tool_ids: list[str] | None = None,
    middleware_ids: list[str] | None = None,
    bump_version: bool = True,
) -> AgentDefinition:
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

    existing = (
        db.query(AgentDefinition)
        .filter(
            AgentDefinition.methodology_id == methodology_id,
            AgentDefinition.name == name,
        )
        .one_or_none()
    )
    if existing is not None:
        raise ValueError(f"方法论内已存在同名 Agent：{name}")

    cfg = dict(config or {})
    cfg.setdefault("role", "subagent")
    cfg.setdefault("enabled", True)

    row = AgentDefinition(
        id=agent_id or f"agent_{uuid.uuid4().hex[:12]}",
        methodology_id=methodology_id,
        name=name,
        system_prompt=system_prompt,
        model=model,
        temperature=temperature,
        config=cfg,
    )
    db.add(row)
    db.flush()

    if tool_ids:
        _bind_tools(db, row.id, tool_ids, replace=True)
    if middleware_ids:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)

    if bump_version:
        _bump_and_snapshot(db, methodology)
    return get_agent(db, row.id)  # type: ignore[return-value]


def update_agent(
    db: Session,
    agent_id: str,
    *,
    name: str | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    config: dict[str, Any] | None = None,
    tool_ids: list[str] | None = None,
    middleware_ids: list[str] | None = None,
    bump_version: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")

    if name is not None:
        row.name = name
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if model is not None:
        row.model = model or None
    if temperature is not None:
        row.temperature = temperature
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if tool_ids is not None:
        _bind_tools(db, row.id, tool_ids, replace=True)
    if middleware_ids is not None:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)

    db.flush()
    if bump_version:
        methodology = db.get(Methodology, row.methodology_id)
        if methodology:
            _bump_and_snapshot(db, methodology)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def delete_agent(db: Session, agent_id: str, *, bump_version: bool = True) -> None:
    row = db.get(AgentDefinition, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    methodology_id = row.methodology_id
    db.delete(row)
    db.flush()
    if bump_version:
        methodology = db.get(Methodology, methodology_id)
        if methodology:
            _bump_and_snapshot(db, methodology)


def bind_agent_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],
    *,
    replace: bool = True,
    bump_version: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    _bind_tools(db, agent_id, tool_ids, replace=replace)
    db.flush()
    if bump_version:
        methodology = db.get(Methodology, row.methodology_id)
        if methodology:
            _bump_and_snapshot(db, methodology)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def bind_agent_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    replace: bool = True,
    bump_version: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    _bind_middlewares(db, agent_id, middleware_ids, replace=replace)
    db.flush()
    if bump_version:
        methodology = db.get(Methodology, row.methodology_id)
        if methodology:
            _bump_and_snapshot(db, methodology)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def _bind_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],
    *,
    replace: bool,
) -> None:
    if replace:
        db.query(AgentTool).filter(AgentTool.agent_id == agent_id).delete()
    for tid in tool_ids:
        tool = db.get(ToolDefinition, tid)
        if tool is None:
            raise LookupError(f"工具不存在：{tid}")
        exists = (
            db.query(AgentTool)
            .filter(AgentTool.agent_id == agent_id, AgentTool.tool_id == tid)
            .one_or_none()
        )
        if exists is None:
            db.add(AgentTool(agent_id=agent_id, tool_id=tid))


def _bind_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    replace: bool,
) -> None:
    if replace:
        db.query(AgentMiddleware).filter(AgentMiddleware.agent_id == agent_id).delete()
    for mid in middleware_ids:
        mw = db.get(MiddlewareDefinition, mid)
        if mw is None:
            raise LookupError(f"中间件不存在：{mid}")
        exists = (
            db.query(AgentMiddleware)
            .filter(
                AgentMiddleware.agent_id == agent_id,
                AgentMiddleware.middleware_id == mid,
            )
            .one_or_none()
        )
        if exists is None:
            db.add(AgentMiddleware(agent_id=agent_id, middleware_id=mid))


def _bump_and_snapshot(db: Session, methodology: Methodology) -> None:
    methodology.version += 1
    methodology.updated_time = datetime.now(timezone.utc)
    invalidate_agent_cache(methodology.id)
    db.flush()
    snapshot_methodology(db, methodology.id)
