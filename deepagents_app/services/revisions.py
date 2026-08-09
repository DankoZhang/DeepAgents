"""
方法论版本快照
==============

职责：
- 把 live 方法论（含 Agent / Tool / Middleware / Skill / LLM）序列化进
  ``MethodologyRevision``，旧会话可按创建时 version 重建 Agent
- 配置变更时统一 ``bump_methodology``：
  - draft：不升版、不写快照（draft 无人读）；仅刷新时间戳并失效缓存
  - published：升版 + 新快照，并按保留策略裁剪历史
- Agent / 模型 / 工具 / Skill 变更时，级联 bump 所有引用它们的方法论
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import NotFoundError
from deepagents_app.config import get_settings
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import (
    AgentDefinition,
    AgentSkill,
    AgentTool,
    Conversation,
    Methodology,
    MethodologyAgent,
    MethodologyRevision,
)
from deepagents_app.services.snapshots import serialize_agent_for_snapshot

logger = logging.getLogger(__name__)

__all__ = [
    "serialize_methodology",
    "snapshot_methodology",
    "get_revision",
    "list_revisions",
    "schedule_cache_invalidation",
    "flush_cache_invalidations",
    "schedule_cache_invalidation_for_agent_ids",
    "prune_methodology_revisions",
    "bump_methodology",
    "bump_methodologies_for_agent_ids",
    "bump_methodologies_using_resource",
    "bump_methodologies_using_agent",
    "bump_methodologies_using_model",
    "bump_methodologies_using_tool",
    "bump_methodologies_using_skill",
]


async def serialize_methodology(db: AsyncSession, methodology_id: str) -> dict[str, Any]:
    """把当前方法论完整配置序列化为可重建的 JSON（含版本化 Memory）。"""
    methodology = (
        await db.scalars(
            select(Methodology)
            .options(*methodology_with_agents_options())
            .where(Methodology.id == methodology_id)
        )
    ).one_or_none()
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    from deepagents_app.services.memory import memory_payload_for_snapshot_async

    return {
        "id": methodology.id,
        "name": methodology.name,
        "description": methodology.description,
        "version": methodology.version,
        "status": methodology.status,
        "memory": await memory_payload_for_snapshot_async(db, get_settings()),
        "agents": [
            await serialize_agent_for_snapshot(db, agent)
            for agent in methodology.agents
        ],
    }


async def snapshot_methodology(db: AsyncSession, methodology_id: str) -> MethodologyRevision:
    """按当前 ``methodology.version`` 写入/覆盖快照（同 version 重复调用会覆盖）。"""
    methodology = await db.get(Methodology, methodology_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    payload = await serialize_methodology(db, methodology_id)
    existing = (
        await db.scalars(
            select(MethodologyRevision).where(
                MethodologyRevision.methodology_id == methodology_id,
                MethodologyRevision.version == methodology.version,
            )
        )
    ).one_or_none()
    if existing is not None:
        # 同 version 覆盖：例如发布时再钉一次，避免多插一行
        existing.snapshot = payload
        existing.created_time = datetime.now(timezone.utc)
        await db.flush()
        return existing

    row = MethodologyRevision(
        id=f"{methodology_id}_v{methodology.version}",
        methodology_id=methodology_id,
        version=methodology.version,
        snapshot=payload,
    )
    db.add(row)
    await db.flush()
    return row


async def get_revision(
    db: AsyncSession,
    methodology_id: str,
    version: int,
) -> MethodologyRevision | None:
    """按方法论 + 版本取快照行（Agent Factory 重建旧会话时用）。"""
    return (
        await db.scalars(
            select(MethodologyRevision).where(
                MethodologyRevision.methodology_id == methodology_id,
                MethodologyRevision.version == version,
            )
        )
    ).one_or_none()


async def list_revisions(db: AsyncSession, methodology_id: str) -> list[MethodologyRevision]:
    """列出方法论全部历史快照，版本号降序。"""
    return list(
        await db.scalars(
            select(MethodologyRevision)
            .where(MethodologyRevision.methodology_id == methodology_id)
            .order_by(MethodologyRevision.version.desc())
        )
    )


# ── 缓存失效：事务内登记，commit 后真正清空 ────────────────────────────


def schedule_cache_invalidation(
    db: AsyncSession,
    methodology_id: str | None = None,
    *,
    all_keys: bool = False,
) -> None:
    """
    登记 Agent 缓存失效意图到 ``db.info``。

    真正执行推迟到事务 commit 之后（见 ``flush_cache_invalidations``），
    避免 rollback 后缓存已被误清。
    """
    info = db.info
    if all_keys or methodology_id is None:
        info["invalidate_agent_cache_all"] = True
        return
    pending: set[str] = info.setdefault("invalidate_methodology_ids", set())
    pending.add(methodology_id)


def flush_cache_invalidations(db: AsyncSession) -> None:
    """在 Session commit 成功后调用：按登记清空 Agent 缓存并广播。"""
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    info = db.info
    if info.pop("invalidate_agent_cache_all", False):
        invalidate_agent_cache()
        info.pop("invalidate_methodology_ids", None)
        return
    pending = info.pop("invalidate_methodology_ids", None) or set()
    for mid in pending:
        invalidate_agent_cache(mid)


async def schedule_cache_invalidation_for_agent_ids(
    db: AsyncSession, agent_ids: Iterable[str]
) -> bool:
    """
    不升版，仅登记引用给定 Agent 的方法论缓存失效。

    用于 bump_related=False 的目录变更；无引用时不做全量清缓存。
    """
    ids = list({aid for aid in agent_ids if aid})
    if not ids:
        return False
    meth_ids = {
        link.methodology_id
        for link in await db.scalars(
            select(MethodologyAgent).where(MethodologyAgent.agent_id.in_(ids))
        )
    }
    if not meth_ids:
        return False
    for mid in meth_ids:
        schedule_cache_invalidation(db, mid)
    return True


# ── 升版：方法论配置变更的统一收尾 ────────────────────────────────────


async def prune_methodology_revisions(
    db: AsyncSession,
    methodology_id: str,
    *,
    keep: int | None = None,
) -> int:
    """
    裁剪方法论历史快照。

    保留最近 ``keep`` 条，以及仍被 Conversation 引用 / 等于 live.version 的版本。
    被删版本会顺带清掉进程内缓存条目（物化 Skills 按内容寻址保留）。
    """
    keep_n = keep if keep is not None else get_settings().methodology_revision_keep
    methodology = await db.get(Methodology, methodology_id)
    if methodology is None:
        return 0

    protected = {
        c.methodology_version
        for c in await db.scalars(
            select(Conversation).where(Conversation.methodology_id == methodology_id)
        )
    }
    protected.add(methodology.version)

    rows = list(
        await db.scalars(
            select(MethodologyRevision)
            .where(MethodologyRevision.methodology_id == methodology_id)
            .order_by(MethodologyRevision.version.desc())
        )
    )
    kept_recent = 0
    deleted = 0
    for row in rows:
        if row.version in protected:
            continue
        if kept_recent < keep_n:
            kept_recent += 1
            continue
        await db.delete(row)
        deleted += 1

    if deleted:
        schedule_cache_invalidation(db, methodology_id)
        await db.flush()
        # 孤儿 content_blob 由后台 / CLI GC 清理，不阻塞写路径
        logger.info(
            "已裁剪方法论快照 methodology=%s deleted=%s keep=%s",
            methodology_id,
            deleted,
            keep_n,
        )
    return deleted


async def bump_methodology(
    db: AsyncSession,
    methodology: Methodology,
    *,
    force: bool = False,
) -> None:
    """
    配置变更收尾。

    - ``draft`` 且非 ``force``：不升版、不写快照；刷新时间戳并失效缓存
      （draft 无人读快照；publish 时再钉）
    - ``published`` 或 ``force=True``：升版 → 登记缓存失效 → 写新快照 → 裁剪历史
    """
    # 行锁：避免并发 bump 读到同一 version 后双写冲突 / 丢版
    locked = await db.scalar(
        select(Methodology)
        .where(Methodology.id == methodology.id)
        .with_for_update()
    )
    if locked is None:
        return
    if locked.status == "draft" and not force:
        locked.updated_time = datetime.now(timezone.utc)
        schedule_cache_invalidation(db, locked.id)
        await db.flush()
        # 保持调用方手上的对象与 DB 一致
        methodology.updated_time = locked.updated_time
        return

    locked.version += 1
    locked.updated_time = datetime.now(timezone.utc)
    methodology.version = locked.version
    methodology.updated_time = locked.updated_time
    schedule_cache_invalidation(db, locked.id)
    await db.flush()
    await snapshot_methodology(db, locked.id)
    await prune_methodology_revisions(db, locked.id)


async def bump_methodologies_for_agent_ids(
    db: AsyncSession, agent_ids: Iterable[str]
) -> bool:
    """
    升版引用给定 Agent 的全部方法论；返回是否命中至少一个方法论。

    未命中时直接返回：孤立 Agent / 无引用目录变更不应清空其他用户的编译缓存。
    """
    ids = list({aid for aid in agent_ids if aid})
    if not ids:
        return False

    meth_ids = {
        link.methodology_id
        for link in await db.scalars(
            select(MethodologyAgent).where(MethodologyAgent.agent_id.in_(ids))
        )
    }
    if not meth_ids:
        return False

    for mid in meth_ids:
        methodology = await db.get(Methodology, mid)
        if methodology is not None:
            await bump_methodology(db, methodology)
    return True


async def bump_methodologies_using_resource(
    db: AsyncSession,
    *,
    kind: str,
    resource_id: str,
) -> bool:
    """
    按资源类型解析引用该资源的 Agent，再级联 bump 相关方法论。

    ``kind``：``agent`` / ``model`` / ``tool`` / ``skill``。
    返回是否命中至少一个方法论（agent/model）或至少一个关联 Agent（tool/skill）。
    """
    if kind == "agent":
        return await bump_methodologies_for_agent_ids(db, [resource_id])

    if kind == "model":
        agent_ids = [
            a.id
            for a in await db.scalars(
                select(AgentDefinition).where(AgentDefinition.model_id == resource_id)
            )
        ]
        return await bump_methodologies_for_agent_ids(db, agent_ids)

    if kind == "tool":
        agent_ids = [
            link.agent_id
            for link in await db.scalars(
                select(AgentTool).where(AgentTool.tool_id == resource_id)
            )
        ]
        if not agent_ids:
            return False
        return await bump_methodologies_for_agent_ids(db, agent_ids)

    if kind == "skill":
        agent_ids = [
            link.agent_id
            for link in await db.scalars(
                select(AgentSkill).where(AgentSkill.skill_id == resource_id)
            )
        ]
        if not agent_ids:
            return False
        return await bump_methodologies_for_agent_ids(db, agent_ids)

    raise ValueError(f"未知 bump 资源类型：{kind}")


async def bump_methodologies_using_agent(db: AsyncSession, agent_id: str) -> None:
    """找出勾选了该全局 Agent 的全部方法论，逐个升版并快照。"""
    await bump_methodologies_using_resource(db, kind="agent", resource_id=agent_id)


async def bump_methodologies_using_model(db: AsyncSession, model_id: str) -> None:
    """模型超参数变更：bump 所有引用该模型的 Agent 所在方法论。"""
    await bump_methodologies_using_resource(db, kind="model", resource_id=model_id)


async def bump_methodologies_using_tool(db: AsyncSession, tool_id: str) -> bool:
    """升版引用该工具的方法论；返回是否命中至少一个 Agent。"""
    return await bump_methodologies_using_resource(
        db, kind="tool", resource_id=tool_id
    )


async def bump_methodologies_using_skill(db: AsyncSession, skill_id: str) -> bool:
    """升版引用该 Skill 的方法论；返回是否命中至少一个 Agent。"""
    return await bump_methodologies_using_resource(
        db, kind="skill", resource_id=skill_id
    )
