"""
方法论版本快照
==============

职责：
- 把 live 方法论（含 Agent / Tool / Middleware / Skill / LLM）序列化进
  ``MethodologyRevision``，旧会话可按创建时 version 重建 Agent
- 配置变更时统一 ``bump_methodology``：
  - draft：不升版，覆盖当前 version 快照（草稿累积）
  - published：升版 + 新快照，并按保留策略裁剪历史
- Agent / 模型 / 工具 / Skill 变更时，级联 bump 所有引用它们的方法论
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

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
    MiddlewareDefinition,
    SkillDefinition,
    ToolDefinition,
)
from deepagents_app.services.llm_models import serialize_model_for_snapshot

logger = logging.getLogger(__name__)


# ── 快照序列化：live ORM → 可持久化 JSON ──────────────────────────────


def serialize_tool_for_snapshot(row: ToolDefinition) -> dict[str, Any]:
    """钉死工具元信息（含 MCP 连接 config），旧会话不随目录修改漂移。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tool_type": row.tool_type,
        "class_path": row.class_path,
        "requires_hitl": bool(row.requires_hitl),
        "config": dict(row.config or {}),
        "status": row.status,
    }


def serialize_middleware_for_snapshot(row: MiddlewareDefinition) -> dict[str, Any]:
    """钉死中间件 class_path + 构造 config。"""
    return {
        "id": row.id,
        "name": row.name,
        "class_path": row.class_path,
        "config": dict(row.config or {}),
    }


def serialize_skill_for_snapshot(row: SkillDefinition) -> dict[str, Any]:
    """钉死 Skill 正文与元信息，旧会话不随目录修改漂移。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "content": row.content,
        "config": dict(row.config or {}),
        "status": row.status,
    }


def serialize_agent_for_snapshot(
    agent: AgentDefinition,
    *,
    include_llm: bool = True,
) -> dict[str, Any]:
    """live Agent ORM → 快照 / 组装共用的 dict（唯一序列化入口）。"""
    payload: dict[str, Any] = {
        "id": agent.id,
        "name": agent.name,
        "system_prompt": agent.system_prompt,
        "model_id": agent.model_id,
        "config": dict(agent.config or {}),
        "tool_ids": [t.id for t in agent.tools],
        "tools": [serialize_tool_for_snapshot(t) for t in agent.tools],
        "middleware_ids": [m.id for m in agent.middlewares],
        "middlewares": [
            serialize_middleware_for_snapshot(m) for m in agent.middlewares
        ],
        "skill_ids": [s.id for s in agent.skills],
        "skills": [serialize_skill_for_snapshot(s) for s in agent.skills],
    }
    if include_llm:
        payload["llm"] = serialize_model_for_snapshot(agent.llm_model)
    else:
        payload["llm"] = None
    return payload


def serialize_methodology(db: Session, methodology_id: str) -> dict[str, Any]:
    """把当前方法论完整配置序列化为可重建的 JSON。"""
    methodology = (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    return {
        "id": methodology.id,
        "name": methodology.name,
        "description": methodology.description,
        "version": methodology.version,
        "status": methodology.status,
        "agents": [
            serialize_agent_for_snapshot(agent, include_llm=True)
            for agent in methodology.agents
        ],
    }


def snapshot_methodology(db: Session, methodology_id: str) -> MethodologyRevision:
    """按当前 ``methodology.version`` 写入/覆盖快照（同 version 重复调用会覆盖）。"""
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    payload = serialize_methodology(db, methodology_id)
    existing = (
        db.query(MethodologyRevision)
        .filter(
            MethodologyRevision.methodology_id == methodology_id,
            MethodologyRevision.version == methodology.version,
        )
        .one_or_none()
    )
    if existing is not None:
        # 同 version 覆盖：例如发布时再钉一次，避免多插一行
        existing.snapshot = payload
        existing.created_time = datetime.now(timezone.utc)
        db.flush()
        return existing

    row = MethodologyRevision(
        id=f"{methodology_id}_v{methodology.version}",
        methodology_id=methodology_id,
        version=methodology.version,
        snapshot=payload,
    )
    db.add(row)
    db.flush()
    return row


def get_revision(
    db: Session,
    methodology_id: str,
    version: int,
) -> MethodologyRevision | None:
    """按方法论 + 版本取快照行（Agent Factory 重建旧会话时用）。"""
    return (
        db.query(MethodologyRevision)
        .filter(
            MethodologyRevision.methodology_id == methodology_id,
            MethodologyRevision.version == version,
        )
        .one_or_none()
    )


def list_revisions(db: Session, methodology_id: str) -> list[MethodologyRevision]:
    """列出方法论全部历史快照，版本号降序。"""
    return (
        db.query(MethodologyRevision)
        .filter(MethodologyRevision.methodology_id == methodology_id)
        .order_by(MethodologyRevision.version.desc())
        .all()
    )


# ── 缓存失效：事务内登记，commit 后真正清空 ────────────────────────────


def schedule_cache_invalidation(
    db: Session,
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


def flush_cache_invalidations(db: Session) -> None:
    """在 Session commit 成功后调用：按登记清空 Agent 缓存。"""
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    info = db.info
    if info.pop("invalidate_agent_cache_all", False):
        invalidate_agent_cache()
        info.pop("invalidate_methodology_ids", None)
        return
    pending = info.pop("invalidate_methodology_ids", None) or set()
    for mid in pending:
        invalidate_agent_cache(mid)


def schedule_cache_invalidation_for_agent_ids(
    db: Session, agent_ids: Iterable[str]
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
        for link in db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id.in_(ids))
        .all()
    }
    if not meth_ids:
        return False
    for mid in meth_ids:
        schedule_cache_invalidation(db, mid)
    return True


# ── 升版：方法论配置变更的统一收尾 ────────────────────────────────────


def prune_methodology_revisions(
    db: Session,
    methodology_id: str,
    *,
    keep: int | None = None,
) -> int:
    """
    裁剪方法论历史快照。

    保留最近 ``keep`` 条，以及仍被 Conversation 引用 / 等于 live.version 的版本。
    被删版本会顺带清掉进程内缓存条目与物化 Skills 目录。
    """
    keep_n = keep if keep is not None else get_settings().methodology_revision_keep
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        return 0

    protected = {
        c.methodology_version
        for c in db.query(Conversation)
        .filter(Conversation.methodology_id == methodology_id)
        .all()
    }
    protected.add(methodology.version)

    rows = (
        db.query(MethodologyRevision)
        .filter(MethodologyRevision.methodology_id == methodology_id)
        .order_by(MethodologyRevision.version.desc())
        .all()
    )
    kept_recent = 0
    deleted = 0
    for row in rows:
        if row.version in protected:
            continue
        if kept_recent < keep_n:
            kept_recent += 1
            continue
        version = row.version
        db.delete(row)
        deleted += 1
        from deepagents_app.services.agent_factory import (
            invalidate_agent_cache,
            skills_scope,
        )
        from deepagents_app.services.skills import clear_materialized_skills

        invalidate_agent_cache(
            methodology_id,
            version,
            owner_user_id=methodology.owner_user_id,
        )
        try:
            from deepagents_app.workspace import user_workspace_dir

            ws = user_workspace_dir(
                get_settings(), methodology.owner_user_id, ensure=False
            )
            clear_materialized_skills(
                get_settings(),
                scope=skills_scope(methodology_id, version),
                workspace_root=ws,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "裁剪快照后清理物化 Skills 失败 %s v%s: %s",
                methodology_id,
                version,
                exc,
            )

    if deleted:
        db.flush()
        logger.info(
            "已裁剪方法论快照 methodology=%s deleted=%s keep=%s",
            methodology_id,
            deleted,
            keep_n,
        )
    return deleted


def bump_methodology(
    db: Session,
    methodology: Methodology,
    *,
    force: bool = False,
) -> None:
    """
    配置变更收尾。

    - ``draft`` 且非 ``force``：不升版，覆盖当前 version 快照并失效缓存（草稿累积）
    - ``published`` 或 ``force=True``：升版 → 登记缓存失效 → 写新快照 → 裁剪历史
    """
    if methodology.status == "draft" and not force:
        methodology.updated_time = datetime.now(timezone.utc)
        schedule_cache_invalidation(db, methodology.id)
        db.flush()
        snapshot_methodology(db, methodology.id)
        return

    methodology.version += 1
    methodology.updated_time = datetime.now(timezone.utc)
    schedule_cache_invalidation(db, methodology.id)
    db.flush()
    snapshot_methodology(db, methodology.id)
    prune_methodology_revisions(db, methodology.id)


def bump_methodologies_for_agent_ids(db: Session, agent_ids: Iterable[str]) -> bool:
    """
    升版引用给定 Agent 的全部方法论；返回是否命中至少一个方法论。

    未命中时直接返回：孤立 Agent / 无引用目录变更不应清空其他用户的编译缓存。
    """
    ids = list({aid for aid in agent_ids if aid})
    if not ids:
        return False

    meth_ids = {
        link.methodology_id
        for link in db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id.in_(ids))
        .all()
    }
    if not meth_ids:
        return False

    for mid in meth_ids:
        methodology = db.get(Methodology, mid)
        if methodology is not None:
            bump_methodology(db, methodology)
    return True


def bump_methodologies_using_agent(db: Session, agent_id: str) -> None:
    """找出勾选了该全局 Agent 的全部方法论，逐个升版并快照。"""
    bump_methodologies_for_agent_ids(db, [agent_id])


def bump_methodologies_using_model(db: Session, model_id: str) -> None:
    """模型超参数变更：bump 所有引用该模型的 Agent 所在方法论。"""
    agent_ids = [
        a.id
        for a in db.query(AgentDefinition)
        .filter(AgentDefinition.model_id == model_id)
        .all()
    ]
    bump_methodologies_for_agent_ids(db, agent_ids)


def bump_methodologies_using_tool(db: Session, tool_id: str) -> bool:
    """升版引用该工具的方法论；返回是否命中至少一个 Agent。"""
    agent_ids = [
        link.agent_id
        for link in db.query(AgentTool).filter(AgentTool.tool_id == tool_id).all()
    ]
    if not agent_ids:
        return False
    return bump_methodologies_for_agent_ids(db, agent_ids)


def bump_methodologies_using_skill(db: Session, skill_id: str) -> bool:
    """升版引用该 Skill 的方法论；返回是否命中至少一个 Agent。"""
    agent_ids = [
        link.agent_id
        for link in db.query(AgentSkill).filter(AgentSkill.skill_id == skill_id).all()
    ]
    if not agent_ids:
        return False
    return bump_methodologies_for_agent_ids(db, agent_ids)
