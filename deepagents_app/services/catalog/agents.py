#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   agents.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   agents.py

Agent 配置管理
==============

全局 Agent CRUD，以及绑定 Tool / Middleware / Skill。
Agent 本身不隶属单一方法论；方法论通过勾选引用。
``config.enabled`` 表示已启用：参与组装、锁定编辑；主 Agent 启用时发布同名方法论。
变更后默认 ``bump_related``，级联升版所有引用该方法论，保证旧会话可快照重建。
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, ForbiddenError, NotFoundError
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
from deepagents_app.services.catalog.llm_models import get_default_model
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.services.catalog.crud_helpers import (
    ensure_unique_owned_name,
    next_owned_copy_name,
    resolve_resource_id,
)
from deepagents_app.services.versioning.revisions import (
    bump_methodologies_using_resource,
    bump_methodology,
    get_revision,
)


logger = logging.getLogger(__name__)

# 启用后禁止 PATCH / 绑定 / 删除，必须先停用
_ENABLED_LOCK_MESSAGE = "Agent 已启用，请先停用后再编辑"
# PATCH 的 config merge 不得改这两项；只允许走 enable / disable 接口
_PRESERVED_CONFIG_KEYS = ("enabled", "methodology_id")


async def list_agents(
    db: AsyncSession,
    *,
    owner_user_id: str,
    methodology_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
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


def agent_role(agent: AgentDefinition | dict[str, Any]) -> str:
    """读取 ``config.role``；缺省 subagent。"""
    cfg = agent.config if isinstance(agent, AgentDefinition) else agent.get("config")
    return str((cfg or {}).get("role", "subagent")).lower()


def agent_is_enabled(agent: AgentDefinition | dict[str, Any]) -> bool:
    """是否已启用：锁定编辑，且主 Agent 对应方法论应为 published。

    未写 ``enabled`` 视为未启用。发布与组装共用本函数，缺字段不得参与编译。
    """
    cfg = agent.config if isinstance(agent, AgentDefinition) else agent.get("config")
    return bool((cfg or {}).get("enabled"))


def _reject_if_enabled(agent: AgentDefinition) -> None:
    """已启用则拒绝修改，避免改到正在被会话引用的配置。"""
    if agent_is_enabled(agent):
        raise BusinessError(_ENABLED_LOCK_MESSAGE)


def _subagent_ids_of(agent: AgentDefinition) -> list[str]:
    """读取主 Agent ``config.subagent_ids``，去空、去重、保序。"""
    raw = (agent.config or {}).get("subagent_ids") or []
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        sid = str(item).strip()
        if not sid or sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _set_enabled(agent: AgentDefinition, enabled: bool, **extra: Any) -> None:
    """写入 ``config.enabled``；``extra`` 用于同时记下 ``methodology_id``。"""
    cfg = dict(agent.config or {})
    cfg["enabled"] = enabled
    cfg.update(extra)
    agent.config = cfg


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

    # role/enabled 存在 config JSON：默认子 Agent、未启用（可编辑、不参与组装）
    cfg = dict(config or {})
    cfg.setdefault("role", "subagent")
    cfg.setdefault("enabled", False)

    row = AgentDefinition(
        id=resolve_resource_id(agent_id, prefix="agent_", label="agent id"),
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
        await bump_methodologies_using_resource(db, kind="agent", resource_id=row.id)
    return await get_agent(db, row.id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def copy_agent(
    db: AsyncSession,
    agent_id: str,
    *,
    owner_user_id: str,
) -> AgentDefinition:
    """
    复制 Agent：配置 / Prompt / 模型 / 绑定原样拷贝，仅名称加 ``_new``。

    副本始终未启用，并去掉 ``methodology_id``，避免和源主 Agent 抢同一方法论。
    """
    source = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if source is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    name = await next_owned_copy_name(
        db,
        AgentDefinition,
        owner_user_id=owner_user_id,
        source_name=source.name,
        label="Agent",
    )
    cfg = deepcopy(dict(source.config or {}))
    cfg["enabled"] = False
    cfg.pop("methodology_id", None)
    return await create_agent(
        db,
        owner_user_id=owner_user_id,
        name=name,
        system_prompt=source.system_prompt,
        model_id=source.model_id,
        config=cfg,
        tool_ids=[t.id for t in source.tools],
        middleware_ids=[m.id for m in source.middlewares],
        skill_ids=[s.id for s in source.skills],
        bump_related=False,
    )


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
    _reject_if_enabled(row)

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
        if agent_role(row) == "supervisor":
            await _sync_supervisor_methodology_name(
                db, row, owner_user_id=owner_user_id
            )
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if model_id is not None:
        row.model_id = await _resolve_model_id_for_user(
            db, model_id, owner_user_id=owner_user_id
        )
    if config is not None:
        # 客户端即使传入 enabled / methodology_id 也丢弃，以库内值为准
        incoming = dict(config)
        for key in _PRESERVED_CONFIG_KEYS:
            incoming.pop(key, None)
        merged = dict(row.config or {})
        merged.update(incoming)
        for key in _PRESERVED_CONFIG_KEYS:
            if key in (row.config or {}):
                merged[key] = row.config[key]
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
        await bump_methodologies_using_resource(
            db, kind="agent", resource_id=agent_id
        )
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
    _reject_if_enabled(row)
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


async def enable_agent(
    db: AsyncSession, agent_id: str, *, owner_user_id: str
) -> AgentDefinition:
    """
    启用 Agent：锁定编辑。

    主 Agent（supervisor）同时用当前自身 + ``config.subagent_ids``
    创建或更新同名方法论并发布。子 Agent 只把 ``enabled`` 置为 True。
    """
    row = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    if agent_is_enabled(row):
        return row
    if agent_role(row) == "supervisor":
        await _enable_supervisor(db, row, owner_user_id=owner_user_id)
    else:
        # 子 Agent 只改自身；已发布方法论需升版，下次组装才纳入
        _set_enabled(row, True)
        await db.flush()
        await bump_methodologies_using_resource(
            db, kind="agent", resource_id=agent_id
        )
    return await get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def disable_agent(
    db: AsyncSession, agent_id: str, *, owner_user_id: str
) -> AgentDefinition:
    """停用 Agent：解锁编辑。主 Agent 同时将关联方法论退回 draft。"""
    row = await get_agent(db, agent_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Agent 不存在：{agent_id}")
    if not agent_is_enabled(row):
        return row
    if agent_role(row) == "supervisor":
        await _disable_supervisor(db, row, owner_user_id=owner_user_id)
    else:
        # 停用后从后续组装中排除，已发布方法论同样升版
        _set_enabled(row, False)
        await db.flush()
        await bump_methodologies_using_resource(
            db, kind="agent", resource_id=agent_id
        )
    return await get_agent(db, agent_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def _enable_supervisor(
    db: AsyncSession, supervisor: AgentDefinition, *, owner_user_id: str
) -> None:
    """
    主 Agent 启用：保证存在同名方法论并发布。

    成员 = 自身 + ``config.subagent_ids``。发布校验只统计 enabled Agent，
    因此必须先把 supervisor.enabled 置 True。若当前 version 已有快照，
    先强制升版，避免覆盖旧会话锁定的那一版。
    """
    from deepagents_app.services.catalog import methodology as methodology_svc

    member_ids = await _supervisor_member_ids(
        db, supervisor, owner_user_id=owner_user_id
    )
    methodology = await _resolve_supervisor_methodology(
        db, supervisor, owner_user_id=owner_user_id
    )
    if methodology is None:
        # 种子可预写 methodology_id，创建时复用该主键
        preferred_id = str((supervisor.config or {}).get("methodology_id") or "") or None
        methodology = await methodology_svc.create_methodology(
            db,
            owner_user_id=owner_user_id,
            name=supervisor.name,
            description=str((supervisor.config or {}).get("description") or ""),
            methodology_id=preferred_id,
            agent_ids=member_ids,
        )
    else:
        if methodology.name != supervisor.name:
            methodology = await methodology_svc.update_methodology(
                db,
                methodology.id,
                owner_user_id=owner_user_id,
                name=supervisor.name,
            )
        methodology = await methodology_svc.bind_methodology_agents(
            db,
            methodology.id,
            member_ids,
            owner_user_id=owner_user_id,
            replace=True,
            bump_version=False,
        )
    _set_enabled(supervisor, True, methodology_id=methodology.id)
    await db.flush()
    prior = await get_revision(db, methodology.id, methodology.version)
    if prior is not None:
        await bump_methodology(db, methodology, force=True)
    await methodology_svc.publish_methodology(
        db, methodology.id, owner_user_id=owner_user_id
    )


async def _disable_supervisor(
    db: AsyncSession, supervisor: AgentDefinition, *, owner_user_id: str
) -> None:
    """
    主 Agent 停用：先把方法论退回 draft，再关 ``enabled``。

    顺序不能反：若 published 方法论里 supervisor 已 disabled，组装会报缺少 Supervisor。
    旧会话仍按锁定 version 读快照，不受影响。
    """
    from deepagents_app.services.catalog import methodology as methodology_svc

    mid = str((supervisor.config or {}).get("methodology_id") or "")
    if mid:
        try:
            await methodology_svc.unpublish_methodology(
                db, mid, owner_user_id=owner_user_id
            )
        except NotFoundError:
            logger.warning(
                "停用主 Agent 时方法论不存在，仅关闭 enabled：agent=%s methodology=%s",
                supervisor.id,
                mid,
            )
    _set_enabled(supervisor, False)
    await db.flush()


async def _supervisor_member_ids(
    db: AsyncSession, supervisor: AgentDefinition, *, owner_user_id: str
) -> list[str]:
    """主 Agent + 配置中的子 Agent；校验归属与角色。"""
    ids = [supervisor.id]
    for sid in _subagent_ids_of(supervisor):
        if sid == supervisor.id:
            raise BusinessError("子 Agent 列表不能包含主 Agent 自身")
        sub = await db.get(AgentDefinition, sid)
        if sub is None:
            raise NotFoundError(f"子 Agent 不存在：{sid}")
        if sub.owner_user_id != owner_user_id:
            raise ForbiddenError(f"子 Agent 不属于当前用户：{sid}")
        if agent_role(sub) == "supervisor":
            raise BusinessError(f"不能把主 Agent 当作子 Agent 绑定：{sub.name}")
        ids.append(sid)
    return ids


async def _resolve_supervisor_methodology(
    db: AsyncSession, supervisor: AgentDefinition, *, owner_user_id: str
) -> Methodology | None:
    """优先 ``config.methodology_id``，否则按同名方法论复用（名称与主 Agent 一致）。"""
    mid = str((supervisor.config or {}).get("methodology_id") or "")
    if mid:
        row = await db.get(Methodology, mid)
        if row is not None and row.owner_user_id == owner_user_id:
            return row
        if row is not None:
            raise ForbiddenError(f"方法论不属于当前用户：{mid}")
    found = (
        await db.scalars(
            select(Methodology).where(
                Methodology.owner_user_id == owner_user_id,
                Methodology.name == supervisor.name,
            )
        )
    ).one_or_none()
    return found


async def _sync_supervisor_methodology_name(
    db: AsyncSession, supervisor: AgentDefinition, *, owner_user_id: str
) -> None:
    """停用态改主 Agent 名称时，同步关联方法论名称。"""
    from deepagents_app.services.catalog import methodology as methodology_svc

    mid = str((supervisor.config or {}).get("methodology_id") or "")
    if not mid:
        return
    methodology = await db.get(Methodology, mid)
    if methodology is None or methodology.owner_user_id != owner_user_id:
        return
    if methodology.name == supervisor.name:
        return
    await methodology_svc.update_methodology(
        db,
        mid,
        owner_user_id=owner_user_id,
        name=supervisor.name,
    )


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
    _reject_if_enabled(row)
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
        await bump_methodologies_using_resource(
            db, kind="agent", resource_id=agent_id
        )
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
    """解析并校验 model_id；缺省取当前用户标记为默认的目录模型。"""
    if not model_id:
        default = await get_default_model(db, owner_user_id=owner_user_id)
        if default is None:
            raise BusinessError("未指定模型且当前用户没有默认模型，请先设置默认模型")
        model_id = default.id
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
