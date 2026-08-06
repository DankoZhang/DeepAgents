"""Agent 配置管理：全局 Agent CRUD + 绑定 Tool / Middleware / Skill。"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.constants import DEFAULT_MODEL_ID
from deepagents_app.db.loading import agent_detail_options
from deepagents_app.db.models import (
    AgentDefinition,
    Methodology,
    MethodologyAgent,
    MiddlewareDefinition,
    ModelDefinition,
    SkillDefinition,
    ToolDefinition,
)
from deepagents_app.ownership import default_model_id_for_user
from deepagents_app.services.revisions import (
    bump_methodologies_using_agent,
    bump_methodology,
)


def list_agents(
    db: Session,
    *,
    owner_user_id: str,
    methodology_id: str | None = None,
) -> list[AgentDefinition]:
    """列出全局 Agent；若传 methodology_id 则只返回该方法论已勾选的。"""
    q = (
        db.query(AgentDefinition)
        .options(*agent_detail_options())
        .filter(AgentDefinition.owner_user_id == owner_user_id)
    )
    if methodology_id:
        q = q.join(MethodologyAgent).filter(
            MethodologyAgent.methodology_id == methodology_id
        )
    return q.order_by(AgentDefinition.name).all()


def get_agent(
    db: Session, agent_id: str, *, owner_user_id: str
) -> AgentDefinition | None:
    """按主键取单个全局 Agent，并带上 tools / middlewares / skills / llm_model。"""
    row = (
        db.query(AgentDefinition)
        .options(*agent_detail_options())
        .filter(
            AgentDefinition.id == agent_id,
            AgentDefinition.owner_user_id == owner_user_id,
        )
        .one_or_none()
    )
    return row


def create_agent(
    db: Session,
    *,
    owner_user_id: str,
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
        .filter(
            AgentDefinition.owner_user_id == owner_user_id,
            AgentDefinition.name == name,
        )
        .one_or_none()
    )
    if existing is not None:
        raise BusinessError(f"已存在同名 Agent：{name}")

    resolved_model_id = _resolve_model_id_for_user(
        db, model_id, owner_user_id=owner_user_id
    )

    cfg = dict(config or {})
    cfg.setdefault("role", "subagent")
    cfg.setdefault("enabled", True)

    row = AgentDefinition(
        id=agent_id or f"agent_{uuid.uuid4().hex[:12]}",
        owner_user_id=owner_user_id,
        name=name,
        system_prompt=system_prompt,
        model_id=resolved_model_id,
        config=cfg,
    )
    db.add(row)
    db.flush()

    if tool_ids:
        _set_agent_tools(db, row, tool_ids, owner_user_id=owner_user_id)
    if middleware_ids:
        _set_agent_middlewares(
            db, row, middleware_ids, owner_user_id=owner_user_id
        )
    if skill_ids:
        _set_agent_skills(db, row, skill_ids, owner_user_id=owner_user_id)

    if bump_related:
        bump_methodologies_using_agent(db, row.id)
    return get_agent(db, row.id, owner_user_id=owner_user_id)  # type: ignore[return-value]


def update_agent(
    db: Session,
    agent_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    system_prompt: str | None = None,
    model_id: str | None = None,
    config: dict[str, Any] | None = None,
    tool_ids: list[str] | None = None,
    middleware_ids: list[str] | None = None,
    skill_ids: list[str] | None = None,
    bump_related: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")

    if name is not None and name != row.name:
        clash = (
            db.query(AgentDefinition)
            .filter(
                AgentDefinition.owner_user_id == owner_user_id,
                AgentDefinition.name == name,
                AgentDefinition.id != agent_id,
            )
            .one_or_none()
        )
        if clash is not None:
            raise BusinessError(f"已存在同名 Agent：{name}")
        row.name = name
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if model_id is not None:
        row.model_id = _resolve_model_id_for_user(
            db, model_id, owner_user_id=owner_user_id
        )
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if tool_ids is not None:
        _set_agent_tools(db, row, tool_ids, owner_user_id=owner_user_id)
    if middleware_ids is not None:
        _set_agent_middlewares(
            db, row, middleware_ids, owner_user_id=owner_user_id
        )
    if skill_ids is not None:
        _set_agent_skills(db, row, skill_ids, owner_user_id=owner_user_id)

    db.flush()
    if bump_related:
        bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


def delete_agent(
    db: Session,
    agent_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    row = get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
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
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        tool_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        setter=_set_agent_tools,
        merger=_merge_agent_tools,
    )


def bind_agent_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        middleware_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        setter=_set_agent_middlewares,
        merger=_merge_agent_middlewares,
    )


def bind_agent_skills(
    db: Session,
    agent_id: str,
    skill_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    return _bind_and_reload(
        db,
        agent_id,
        skill_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        setter=_set_agent_skills,
        merger=_merge_agent_skills,
    )


def _bind_and_reload(
    db: Session,
    agent_id: str,
    target_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool,
    bump_related: bool,
    setter,
    merger,
) -> AgentDefinition:
    row = get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    if replace:
        setter(db, row, target_ids, owner_user_id=owner_user_id)
    else:
        merger(db, row, target_ids, owner_user_id=owner_user_id)
    db.flush()
    if bump_related:
        bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


def _load_owned(
    db: Session,
    model: type,
    target_id: str,
    *,
    owner_user_id: str,
    missing_label: str,
) -> Any:
    target = db.get(model, target_id)
    if target is None:
        raise NotFoundError(f"{missing_label}不存在：{target_id}")
    if target.owner_user_id != owner_user_id:
        raise BusinessError(f"{missing_label}不属于当前用户：{target_id}")
    return target


def _set_agent_tools(
    db: Session,
    agent: AgentDefinition,
    tool_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    tools = [
        _load_owned(
            db, ToolDefinition, tid, owner_user_id=owner_user_id, missing_label="工具"
        )
        for tid in tool_ids
    ]
    agent.tools = tools


def _merge_agent_tools(
    db: Session,
    agent: AgentDefinition,
    tool_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    existing = {t.id for t in agent.tools}
    for tid in tool_ids:
        if tid in existing:
            continue
        agent.tools.append(
            _load_owned(
                db,
                ToolDefinition,
                tid,
                owner_user_id=owner_user_id,
                missing_label="工具",
            )
        )


def _set_agent_middlewares(
    db: Session,
    agent: AgentDefinition,
    middleware_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    rows = [
        _load_owned(
            db,
            MiddlewareDefinition,
            mid,
            owner_user_id=owner_user_id,
            missing_label="中间件",
        )
        for mid in middleware_ids
    ]
    agent.middlewares = rows


def _merge_agent_middlewares(
    db: Session,
    agent: AgentDefinition,
    middleware_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    existing = {m.id for m in agent.middlewares}
    for mid in middleware_ids:
        if mid in existing:
            continue
        agent.middlewares.append(
            _load_owned(
                db,
                MiddlewareDefinition,
                mid,
                owner_user_id=owner_user_id,
                missing_label="中间件",
            )
        )


def _set_agent_skills(
    db: Session,
    agent: AgentDefinition,
    skill_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    rows = []
    for sid in skill_ids:
        skill = _load_owned(
            db,
            SkillDefinition,
            sid,
            owner_user_id=owner_user_id,
            missing_label="Skill",
        )
        if skill.status != "active":
            raise BusinessError(f"Skill 已禁用：{skill.name}")
        rows.append(skill)
    agent.skills = rows


def _merge_agent_skills(
    db: Session,
    agent: AgentDefinition,
    skill_ids: list[str],
    *,
    owner_user_id: str,
) -> None:
    existing = {s.id for s in agent.skills}
    for sid in skill_ids:
        if sid in existing:
            continue
        skill = _load_owned(
            db,
            SkillDefinition,
            sid,
            owner_user_id=owner_user_id,
            missing_label="Skill",
        )
        if skill.status != "active":
            raise BusinessError(f"Skill 已禁用：{skill.name}")
        agent.skills.append(skill)


def _resolve_model_id_for_user(
    db: Session,
    model_id: str | None,
    *,
    owner_user_id: str,
) -> str | None:
    """解析并校验 model_id；None / 默认 base id 映射为该用户 scoped 默认模型。"""
    if not model_id or model_id == DEFAULT_MODEL_ID:
        model_id = default_model_id_for_user(owner_user_id)
    return _validate_model_id(db, model_id, owner_user_id=owner_user_id)


def _validate_model_id(
    db: Session, model_id: str | None, *, owner_user_id: str
) -> str | None:
    """校验 model_id 存在且可用；None 表示不绑定目录。"""
    if not model_id:
        return None
    row = db.get(ModelDefinition, model_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")
    if row.owner_user_id != owner_user_id:
        raise BusinessError(f"模型不属于当前用户：{model_id}")
    if row.status != "active":
        raise BusinessError(f"模型已禁用：{row.name}")
    return model_id
