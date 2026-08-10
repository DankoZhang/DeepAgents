"""
Agent 配置管理
==============

全局 Agent CRUD，以及绑定 Tool / Middleware / Skill。
Agent 本身不隶属单一方法论；方法论通过勾选引用。
变更后默认 ``bump_related``，级联升版所有引用该方法论，保证旧会话可快照重建。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, ForbiddenError, NotFoundError
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
from deepagents_app.ownership import default_model_id_for_user, validate_resource_id
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.services.crud_helpers import ensure_unique_owned_name
from deepagents_app.services.revisions import (
    bump_methodologies_using_agent,
    bump_methodology,
)


async def list_agents(
    db: AsyncSession,
    *,
    owner_user_id: str,
    methodology_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    cursor: str | None = None,
) -> tuple[list[AgentDefinition], int, str | None]:
    """列出全局 Agent；若传 methodology_id 则只返回该方法论已勾选的。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(AgentDefinition)
        .options(*agent_detail_options())
        .where(AgentDefinition.owner_user_id == owner_user_id)
    )
    if methodology_id:
        stmt = stmt.join(MethodologyAgent).where(
            MethodologyAgent.methodology_id == methodology_id
        )
    stmt = stmt.order_by(AgentDefinition.name, AgentDefinition.id)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        offset=offset,
        cursor=cursor,
        sort_column=AgentDefinition.name,
        id_column=AgentDefinition.id,
        sort_attr="name",
    )


async def get_agent(
    db: AsyncSession, agent_id: str, *, owner_user_id: str
) -> AgentDefinition | None:
    """按主键取单个全局 Agent，并带上 tools / middlewares / skills / llm_model。"""
    return (
        await db.scalars(
            select(AgentDefinition)
            .options(*agent_detail_options())
            .where(
                AgentDefinition.id == agent_id,
                AgentDefinition.owner_user_id == owner_user_id,
            )
        )
    ).one_or_none()


async def create_agent(
    db: AsyncSession,
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

    await ensure_unique_owned_name(
        db,
        AgentDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="Agent",
    )

    resolved_model_id = await _resolve_model_id_for_user(
        db, model_id, owner_user_id=owner_user_id
    )

    # role/enabled 存在 config JSON：默认子 Agent 且启用
    cfg = dict(config or {})
    cfg.setdefault("role", "subagent")
    cfg.setdefault("enabled", True)

    row = AgentDefinition(
        id=_resolve_agent_id(agent_id),
        owner_user_id=owner_user_id,
        name=name,
        system_prompt=system_prompt,
        model_id=resolved_model_id,
        config=cfg,
    )
    db.add(row)
    await db.flush()

    if tool_ids:
        await _set_agent_relations(
            db,
            row,
            tool_ids,
            owner_user_id=owner_user_id,
            model=ToolDefinition,
            attr="tools",
            label="工具",
        )
    if middleware_ids:
        await _set_agent_relations(
            db,
            row,
            middleware_ids,
            owner_user_id=owner_user_id,
            model=MiddlewareDefinition,
            attr="middlewares",
            label="中间件",
        )
    if skill_ids:
        await _set_agent_relations(
            db,
            row,
            skill_ids,
            owner_user_id=owner_user_id,
            model=SkillDefinition,
            attr="skills",
            label="Skill",
            require_active=True,
        )

    if bump_related:
        await bump_methodologies_using_agent(db, row.id)
    return await get_agent(db, row.id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def update_agent(
    db: AsyncSession,
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
    """更新全局 Agent；字段为 None 表示不改。改完可级联 bump 引用方法论。"""
    row = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")

    if name is not None and name != row.name:
        await ensure_unique_owned_name(
            db,
            AgentDefinition,
            owner_user_id=owner_user_id,
            name=name,
            exclude_id=agent_id,
            label="Agent",
        )
        row.name = name
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if model_id is not None:
        row.model_id = await _resolve_model_id_for_user(
            db, model_id, owner_user_id=owner_user_id
        )
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if tool_ids is not None:
        await _set_agent_relations(
            db,
            row,
            tool_ids,
            owner_user_id=owner_user_id,
            model=ToolDefinition,
            attr="tools",
            label="工具",
        )
    if middleware_ids is not None:
        await _set_agent_relations(
            db,
            row,
            middleware_ids,
            owner_user_id=owner_user_id,
            model=MiddlewareDefinition,
            attr="middlewares",
            label="中间件",
        )
    if skill_ids is not None:
        await _set_agent_relations(
            db,
            row,
            skill_ids,
            owner_user_id=owner_user_id,
            model=SkillDefinition,
            attr="skills",
            label="Skill",
            require_active=True,
        )

    await db.flush()
    if bump_related:
        await bump_methodologies_using_agent(db, agent_id)
    return await get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def delete_agent(
    db: AsyncSession,
    agent_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """
    删除全局 Agent。

    删除前先记下引用该方法论；关联行级联消失后，再对那些方法论升版打快照。
    """
    row = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    # 删除后 MethodologyAgent 会级联没掉，必须先收集
    methodology_ids = [
        r.methodology_id
        for r in await db.scalars(
            select(MethodologyAgent).where(MethodologyAgent.agent_id == agent_id)
        )
    ]
    await db.delete(row)
    await db.flush()
    if bump_related:
        for mid in methodology_ids:
            methodology = await db.get(Methodology, mid)
            if methodology:
                await bump_methodology(db, methodology)


async def bind_agent_tools(
    db: AsyncSession,
    agent_id: str,
    tool_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    """绑定工具：replace=True 全量替换，False 增量追加。"""
    return await _bind_and_reload(
        db,
        agent_id,
        tool_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        model=ToolDefinition,
        attr="tools",
        label="工具",
    )


async def bind_agent_middlewares(
    db: AsyncSession,
    agent_id: str,
    middleware_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    """绑定中间件：replace=True 全量替换，False 增量追加。"""
    return await _bind_and_reload(
        db,
        agent_id,
        middleware_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        model=MiddlewareDefinition,
        attr="middlewares",
        label="中间件",
    )


async def bind_agent_skills(
    db: AsyncSession,
    agent_id: str,
    skill_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    """绑定 Skill：仅允许 active；replace=True 全量替换，False 增量追加。"""
    return await _bind_and_reload(
        db,
        agent_id,
        skill_ids,
        owner_user_id=owner_user_id,
        replace=replace,
        bump_related=bump_related,
        model=SkillDefinition,
        attr="skills",
        label="Skill",
        require_active=True,
    )


# ── 绑定辅助：工具 / 中间件 / Skill 共用一套 set / merge ───────────────


async def _bind_and_reload(
    db: AsyncSession,
    agent_id: str,
    target_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool,
    bump_related: bool,
    model: type,
    attr: str,
    label: str,
    require_active: bool = False,
) -> AgentDefinition:
    """写关联 → 可选 bump → 重新加载详情。"""
    row = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    if replace:
        await _set_agent_relations(
            db,
            row,
            target_ids,
            owner_user_id=owner_user_id,
            model=model,
            attr=attr,
            label=label,
            require_active=require_active,
        )
    else:
        await _merge_agent_relations(
            db,
            row,
            target_ids,
            owner_user_id=owner_user_id,
            model=model,
            attr=attr,
            label=label,
            require_active=require_active,
        )
    await db.flush()
    if bump_related:
        await bump_methodologies_using_agent(db, agent_id)
    return await get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def _load_owned(
    db: AsyncSession,
    model: type,
    target_id: str,
    *,
    owner_user_id: str,
    missing_label: str,
    require_active: bool = False,
) -> Any:
    """加载并校验归属：只能绑定当前用户自己的目录资源。"""
    target = await db.get(model, target_id)
    if target is None:
        raise NotFoundError(f"{missing_label}不存在：{target_id}")
    if target.owner_user_id != owner_user_id:
        raise ForbiddenError(f"{missing_label}不属于当前用户：{target_id}")
    if require_active and getattr(target, "status", "active") != "active":
        raise BusinessError(f"{missing_label}已禁用：{getattr(target, 'name', target_id)}")
    return target


async def _set_agent_relations(
    db: AsyncSession,
    agent: AgentDefinition,
    target_ids: list[str],
    *,
    owner_user_id: str,
    model: type,
    attr: str,
    label: str,
    require_active: bool = False,
) -> None:
    rows = [
        await _load_owned(
            db,
            model,
            tid,
            owner_user_id=owner_user_id,
            missing_label=label,
            require_active=require_active,
        )
        for tid in target_ids
    ]
    # AsyncSession 禁止隐式 sync lazyload：先 await 加载再替换集合
    await getattr(agent.awaitable_attrs, attr)
    setattr(agent, attr, rows)


async def _merge_agent_relations(
    db: AsyncSession,
    agent: AgentDefinition,
    target_ids: list[str],
    *,
    owner_user_id: str,
    model: type,
    attr: str,
    label: str,
    require_active: bool = False,
) -> None:
    current: list[Any] = list(await getattr(agent.awaitable_attrs, attr))
    existing = {item.id for item in current}
    for tid in target_ids:
        if tid in existing:
            continue
        current.append(
            await _load_owned(
                db,
                model,
                tid,
                owner_user_id=owner_user_id,
                missing_label=label,
                require_active=require_active,
            )
        )
    setattr(agent, attr, current)


# ── 模型绑定：默认模型映射 + 归属/状态校验 ─────────────────────────────


async def _resolve_model_id_for_user(
    db: AsyncSession,
    model_id: str | None,
    *,
    owner_user_id: str,
) -> str | None:
    """解析并校验 model_id；None / 默认 base id 映射为该用户 scoped 默认模型。"""
    if not model_id or model_id == DEFAULT_MODEL_ID:
        model_id = default_model_id_for_user(owner_user_id)
    return await _validate_model_id(db, model_id, owner_user_id=owner_user_id)


async def _validate_model_id(
    db: AsyncSession, model_id: str | None, *, owner_user_id: str
) -> str | None:
    """校验 model_id 存在且可用；None 表示不绑定目录。"""
    if not model_id:
        return None
    row = await db.get(ModelDefinition, model_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")
    if row.owner_user_id != owner_user_id:
        raise BusinessError(f"模型不属于当前用户：{model_id}")
    if row.status != "active":
        raise BusinessError(f"模型已禁用：{row.name}")
    return model_id


def _resolve_agent_id(agent_id: str | None) -> str:
    resolved = agent_id or f"agent_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="agent id")
