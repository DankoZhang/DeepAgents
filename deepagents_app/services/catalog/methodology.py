#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   methodology.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   methodology.py

方法论 CRUD、发布、勾选全局 Agent
================================

版本语义：
- 草稿态改 Agent 勾选 / 被引用 Agent 变更 → 不升版、不写快照（draft 无人读快照）
- 已发布方法论的配置变更 → 升版并写新快照，并按保留策略裁剪历史
- 仅改名称/描述 → 不升版
- 创建草稿时不写快照；发布时钉死当前 version（不额外升版）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, ForbiddenError, NotFoundError
from deepagents_app.db.loading import load_methodology_with_agents
from deepagents_app.db.models import AgentDefinition, Conversation, Methodology
from deepagents_app.ownership import validate_resource_id
from deepagents_app.db.pagination import DEFAULT_LIMIT, coerce_datetime, page_rows
from deepagents_app.services.catalog.agents import agent_is_enabled, agent_role
from deepagents_app.services.catalog.crud_helpers import ensure_unique_owned_name
from deepagents_app.services.versioning.revisions import (
    bump_methodology,
    list_revisions,
    schedule_cache_invalidation,
    snapshot_methodology,
)


async def list_methodologies(
    db: AsyncSession,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Methodology], int, str | None]:
    """按所有者列出方法论；可按 draft/published 过滤。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(Methodology)
        .where(Methodology.owner_user_id == owner_user_id)
        .order_by(Methodology.updated_time.desc(), Methodology.id.desc())
    )
    if status:
        stmt = stmt.where(Methodology.status == status)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        cursor=cursor,
        sort_column=Methodology.updated_time,
        id_column=Methodology.id,
        sort_attr="updated_time",
        descending=True,
        coerce_sort=coerce_datetime,
    )


async def get_methodology(
    db: AsyncSession, methodology_id: str, *, owner_user_id: str
) -> Methodology | None:
    """取单个方法论（含已勾选 Agent 及其 tools/middlewares/skills/llm）。"""
    return await load_methodology_with_agents(
        db, methodology_id, owner_user_id=owner_user_id
    )


async def create_methodology(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    description: str = "",
    methodology_id: str | None = None,
    agent_ids: list[str] | None = None,
) -> Methodology:
    """创建草稿方法论，可选立即勾选全局 Agent（不写快照；发布时再钉）。"""

    await ensure_unique_owned_name(
        db,
        Methodology,
        owner_user_id=owner_user_id,
        name=name,
        label="方法论",
    )

    mid = methodology_id or _slug_id(name)
    mid = validate_resource_id(mid, label="methodology id")
    if await db.get(Methodology, mid) is not None:
        raise BusinessError(f"方法论已存在：{mid}")
    row = Methodology(
        id=mid,
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        version=1,
        status="draft",
    )
    db.add(row)
    await db.flush()
    # 创建阶段不升版、不写快照：draft 无人读快照；publish 时再钉 v1
    if agent_ids:
        await bind_methodology_agents(
            db,
            mid,
            agent_ids,
            owner_user_id=owner_user_id,
            replace=True,
            bump_version=False,
        )
    return await get_methodology(db, mid, owner_user_id=owner_user_id) or row


async def update_methodology(
    db: AsyncSession,
    methodology_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
) -> Methodology:
    """仅更新元信息（名称/描述）；不影响 Agent 组装，不升版。"""
    row = await get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    if name is not None and name != row.name:
        await ensure_unique_owned_name(
            db,
            Methodology,
            owner_user_id=owner_user_id,
            name=name,
            exclude_id=methodology_id,
            label="方法论",
        )
        row.name = name
    if description is not None:
        row.description = description
    row.updated_time = datetime.now(timezone.utc)
    await db.flush()
    return row


async def delete_methodology(
    db: AsyncSession, methodology_id: str, *, owner_user_id: str
) -> None:
    """删除方法论；仍有会话引用时拒绝，避免孤儿 Conversation。"""
    row = await get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    conv_count = await db.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.methodology_id == methodology_id)
    )
    if conv_count:
        raise BusinessError(
            f"方法论仍有 {conv_count} 个会话引用，无法删除；请先删除相关会话"
        )

    schedule_cache_invalidation(db, methodology_id)
    await db.delete(row)
    await db.flush()


async def bind_methodology_agents(
    db: AsyncSession,
    methodology_id: str,
    agent_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_version: bool = True,
) -> Methodology:
    """
    方法论勾选 / 替换全局 Agent 列表。

    ``bump_version=True``（默认）：走 ``bump_methodology``——
    draft 不升版、不写快照；published 升版 + 新快照 + 失效缓存。
    ``bump_version=False``：只改关联（如创建草稿时勾选 Agent）。
    """
    methodology = await get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    # 只能勾选当前用户自己的全局 Agent
    agents: list[AgentDefinition] = []
    for aid in agent_ids:
        agent = await db.get(AgentDefinition, aid)
        if agent is None:
            raise NotFoundError(f"Agent 不存在：{aid}")
        if agent.owner_user_id != owner_user_id:
            raise ForbiddenError(f"Agent 不属于当前用户：{aid}")
        agents.append(agent)

    if replace:
        await getattr(methodology.awaitable_attrs, "agents")
        methodology.agents = agents
    else:
        # 增量追加：已有的不重复加入
        existing = {a.id for a in methodology.agents}
        for agent in agents:
            if agent.id not in existing:
                methodology.agents.append(agent)

    if bump_version:
        await bump_methodology(db, methodology)
    else:
        await db.flush()
    return await get_methodology(db, methodology_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


async def publish_methodology(
    db: AsyncSession, methodology_id: str, *, owner_user_id: str
) -> Methodology:
    """发布：enabled Agent 中须恰好一个 Supervisor（与组装口径一致）；发布后才能建会话。"""
    from deepagents_app.services.catalog.roles import require_single_supervisor

    row = await get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    # 与组装共用 agent_role / agent_is_enabled，未写 enabled 视为未启用
    require_single_supervisor(
        list(row.agents),
        context="发布失败",
        role_of=agent_role,
        name_of=lambda a: a.name,
        enabled_of=agent_is_enabled,
    )
    row.status = "published"
    row.updated_time = datetime.now(timezone.utc)
    schedule_cache_invalidation(db, methodology_id)
    await db.flush()
    # 发布瞬间再钉一版快照；复用上方已 eager-load 的 row，避免二次全量查询
    await snapshot_methodology(db, methodology_id, methodology=row)
    return row


async def unpublish_methodology(
    db: AsyncSession, methodology_id: str, *, owner_user_id: str
) -> Methodology:
    """
    停用：status 回到 draft，不删快照、不降 version。

    旧会话仍按 Conversation.methodology_version 重建；新建会话会因
    非 published 被拒绝。产品主路径由主 Agent disable 调用本函数。
    """
    row = await get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    if row.status == "draft":
        return row
    row.status = "draft"
    row.updated_time = datetime.now(timezone.utc)
    # 已缓存的 compiled agent 必须失效，避免继续按 published 组装新请求
    schedule_cache_invalidation(db, methodology_id)
    await db.flush()
    return row


async def get_methodology_versions(
    db: AsyncSession, methodology_id: str, *, owner_user_id: str
) -> list[dict]:
    """列出该方法论历史快照版本（供前端/调试查看）。"""
    if await get_methodology(db, methodology_id, owner_user_id=owner_user_id) is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    return [
        {
            "methodology_id": r.methodology_id,
            "version": r.version,
            "created_time": r.created_time.isoformat(),
        }
        for r in await list_revisions(db, methodology_id)
    ]


def _slug_id(name: str) -> str:
    """由名称生成可读主键：清洗后截断 + 短 uuid 后缀防撞。"""
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    base = base.strip("_")[:48] or "methodology"
    return f"{base}_{uuid.uuid4().hex[:8]}"
