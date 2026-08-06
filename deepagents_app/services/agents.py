"""Agent 配置管理：全局 Agent CRUD + 绑定 Tool / Middleware / Skill。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.constants import DEFAULT_MODEL_ID
from deepagents_app.db.loading import agent_detail_options
from deepagents_app.db.models import (
    AgentDefinition,
    AgentMiddleware,
    AgentSkill,
    AgentTool,
    Methodology,
    MethodologyAgent,
    MiddlewareDefinition,
    ModelDefinition,
    SkillDefinition,
    ToolDefinition,
)
from deepagents_app.services.revisions import (
    bump_methodologies_using_agent,
    bump_methodology,
)


def list_agents(
    db: Session,
    *,
    methodology_id: str | None = None,
) -> list[AgentDefinition]:
    """列出全局 Agent；若传 methodology_id 则只返回该方法论已勾选的。"""
    q = db.query(AgentDefinition).options(*agent_detail_options())
    if methodology_id:
        q = q.join(MethodologyAgent).filter(
            MethodologyAgent.methodology_id == methodology_id
        )
    return q.order_by(AgentDefinition.name).all()


def get_agent(db: Session, agent_id: str) -> AgentDefinition | None:
    """按主键取单个全局 Agent，并带上 tools / middlewares / skills / llm_model。"""
    # 同会话内刚改过 association 时，先 expire 关系集合再 joinedload
    cached = db.get(AgentDefinition, agent_id)
    if cached is not None:
        db.expire(cached, ["tools", "middlewares", "skills", "llm_model"])
    return (
        db.query(AgentDefinition)
        .options(*agent_detail_options())
        .execution_options(populate_existing=True)
        .filter(AgentDefinition.id == agent_id)
        .one_or_none()
    )


def create_agent(
    db: Session,
    *,
    name: str,
    system_prompt: str = "",
    model_id: str | None = None,
    config: dict[str, Any] | None = None,
    agent_id: str | None = None,
    tool_ids: list[str] | None = None,
    middleware_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    bump_related: bool = True,
) -> AgentDefinition:
    """创建全局 Agent（不隶属单一方法论；由方法论另行勾选）。"""
    existing = (
        db.query(AgentDefinition)
        .filter(AgentDefinition.name == name)
        .one_or_none()
    )
    if existing is not None:
        raise ValueError(f"已存在同名 Agent：{name}")

    resolved_model_id = _validate_model_id(db, model_id or DEFAULT_MODEL_ID)

    cfg = dict(config or {})
    cfg.setdefault("role", "subagent")
    cfg.setdefault("enabled", True)

    row = AgentDefinition(
        id=agent_id or f"agent_{uuid.uuid4().hex[:12]}",
        name=name,
        system_prompt=system_prompt,
        model_id=resolved_model_id,
        config=cfg,
    )
    db.add(row)
    db.flush()

    if tool_ids:
        _bind_tools(db, row.id, tool_ids, replace=True)
    if middleware_ids:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)
    if skill_ids:
        _bind_skills(db, row.id, skill_ids, replace=True)

    if bump_related:
        bump_methodologies_using_agent(db, row.id)
    return get_agent(db, row.id)  # type: ignore[return-value]


def update_agent(
    db: Session,
    agent_id: str,
    *,
    name: str | None = None,
    system_prompt: str | None = None,
    model_id: str | None = None,
    config: dict[str, Any] | None = None,
    tool_ids: list[str] | None = None,
    middleware_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    bump_related: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")

    if name is not None and name != row.name:
        clash = (
            db.query(AgentDefinition)
            .filter(AgentDefinition.name == name, AgentDefinition.id != agent_id)
            .one_or_none()
        )
        if clash is not None:
            raise ValueError(f"已存在同名 Agent：{name}")
        row.name = name
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if model_id is not None:
        row.model_id = _validate_model_id(db, model_id or DEFAULT_MODEL_ID)
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if tool_ids is not None:
        _bind_tools(db, row.id, tool_ids, replace=True)
    if middleware_ids is not None:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)
    if skill_ids is not None:
        _bind_skills(db, row.id, skill_ids, replace=True)

    db.flush()
    if bump_related:
        bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def delete_agent(db: Session, agent_id: str, *, bump_related: bool = True) -> None:
    row = db.get(AgentDefinition, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    methodology_ids = [
        r.methodology_id
        for r in db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id == agent_id)
        .all()
    ]
    db.delete(row)
    db.flush()
    if bump_related:
        for mid in methodology_ids:
            methodology = db.get(Methodology, mid)
            if methodology:
                bump_methodology(db, methodology)


def bind_agent_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],
    *,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        tool_ids,
        replace=replace,
        bump_related=bump_related,
        binder=_bind_tools,
    )


def bind_agent_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        middleware_ids,
        replace=replace,
        bump_related=bump_related,
        binder=_bind_middlewares,
    )


def bind_agent_skills(
    db: Session,
    agent_id: str,
    skill_ids: list[str],
    *,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        skill_ids,
        replace=replace,
        bump_related=bump_related,
        binder=_bind_skills,
    )


def _bind_and_reload(
    db: Session,
    agent_id: str,
    target_ids: list[str],
    *,
    replace: bool,
    bump_related: bool,
    binder,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    binder(db, agent_id, target_ids, replace=replace)
    db.flush()
    if bump_related:
        bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def _bind_links(
    db: Session,
    agent_id: str,
    target_ids: list[str],
    *,
    replace: bool,
    link_model: type,
    target_model: type,
    link_fk: str,
    missing_label: str,
    validate_target=None,
) -> None:
    """通用 Agent ↔ 目标实体多对多绑定。"""
    if replace:
        db.query(link_model).filter(link_model.agent_id == agent_id).delete()
    for tid in target_ids:
        target = db.get(target_model, tid)
        if target is None:
            raise LookupError(f"{missing_label}不存在：{tid}")
        if validate_target is not None:
            validate_target(target)
        exists = (
            db.query(link_model)
            .filter_by(agent_id=agent_id, **{link_fk: tid})
            .one_or_none()
        )
        if exists is None:
            db.add(link_model(agent_id=agent_id, **{link_fk: tid}))


def _bind_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],
    *,
    replace: bool,
) -> None:
    _bind_links(
        db,
        agent_id,
        tool_ids,
        replace=replace,
        link_model=AgentTool,
        target_model=ToolDefinition,
        link_fk="tool_id",
        missing_label="工具",
    )


def _bind_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    replace: bool,
) -> None:
    _bind_links(
        db,
        agent_id,
        middleware_ids,
        replace=replace,
        link_model=AgentMiddleware,
        target_model=MiddlewareDefinition,
        link_fk="middleware_id",
        missing_label="中间件",
    )


def _bind_skills(
    db: Session,
    agent_id: str,
    skill_ids: list[str],
    *,
    replace: bool,
) -> None:
    def _check_active(skill: SkillDefinition) -> None:
        if skill.status != "active":
            raise ValueError(f"Skill 已禁用：{skill.name}")

    _bind_links(
        db,
        agent_id,
        skill_ids,
        replace=replace,
        link_model=AgentSkill,
        target_model=SkillDefinition,
        link_fk="skill_id",
        missing_label="Skill",
        validate_target=_check_active,
    )


def _validate_model_id(db: Session, model_id: str | None) -> str | None:
    """校验 model_id 存在且可用；None 表示不绑定目录。"""
    if not model_id:
        return None
    row = db.get(ModelDefinition, model_id)
    if row is None:
        raise LookupError(f"模型不存在：{model_id}")
    if row.status != "active":
        raise ValueError(f"模型已禁用：{row.name}")
    return model_id
