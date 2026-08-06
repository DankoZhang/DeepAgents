"""方法论 CRUD、发布、勾选全局 Agent。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Conversation, Methodology
from deepagents_app.services.revisions import (
    bump_methodology,
    list_revisions,
    schedule_cache_invalidation,
    snapshot_methodology,
)


def list_methodologies(
    db: Session,
    *,
    owner_user_id: str,
    status: str | None = None,
) -> list[Methodology]:
    q = (
        db.query(Methodology)
        .filter(Methodology.owner_user_id == owner_user_id)
        .order_by(Methodology.updated_time.desc())
    )
    if status:
        q = q.filter(Methodology.status == status)
    return q.all()


def get_methodology(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> Methodology | None:
    return (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
        .filter(
            Methodology.id == methodology_id,
            Methodology.owner_user_id == owner_user_id,
        )
        .one_or_none()
    )


def create_methodology(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    description: str = "",
    methodology_id: str | None = None,
    agent_ids: list[str] | None = None,
) -> Methodology:
    """创建草稿方法论，可选立即勾选全局 Agent，并写入 v1 快照。"""
    name_clash = (
        db.query(Methodology)
        .filter(
            Methodology.owner_user_id == owner_user_id,
            Methodology.name == name,
        )
        .one_or_none()
    )
    if name_clash is not None:
        raise BusinessError(f"已存在同名方法论：{name}")

    mid = methodology_id or _slug_id(name)
    if db.get(Methodology, mid) is not None:
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
    db.flush()
    if agent_ids:
        bind_methodology_agents(
            db,
            mid,
            agent_ids,
            owner_user_id=owner_user_id,
            replace=True,
            bump_version=False,
        )
    snapshot_methodology(db, mid)
    return get_methodology(db, mid, owner_user_id=owner_user_id) or row


def update_methodology(
    db: Session,
    methodology_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
) -> Methodology:
    """仅更新元信息（名称/描述）；不影响 Agent 组装，不升版。"""
    row = get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    if name is not None and name != row.name:
        clash = (
            db.query(Methodology)
            .filter(
                Methodology.owner_user_id == owner_user_id,
                Methodology.name == name,
                Methodology.id != methodology_id,
            )
            .one_or_none()
        )
        if clash is not None:
            raise BusinessError(f"已存在同名方法论：{name}")
        row.name = name
    if description is not None:
        row.description = description
    row.updated_time = datetime.now(timezone.utc)
    db.flush()
    return row


def delete_methodology(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> None:
    row = get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    conv_count = (
        db.query(Conversation)
        .filter(Conversation.methodology_id == methodology_id)
        .count()
    )
    if conv_count:
        raise BusinessError(
            f"方法论仍有 {conv_count} 个会话引用，无法删除；请先删除相关会话"
        )

    schedule_cache_invalidation(db, methodology_id)
    db.delete(row)
    db.flush()


def bind_methodology_agents(
    db: Session,
    methodology_id: str,
    agent_ids: list[str],
    *,
    owner_user_id: str,
    replace: bool = True,
    bump_version: bool = True,
) -> Methodology:
    """方法论勾选 / 替换全局 Agent 列表。"""
    methodology = get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    agents: list[AgentDefinition] = []
    for aid in agent_ids:
        agent = db.get(AgentDefinition, aid)
        if agent is None:
            raise NotFoundError(f"Agent 不存在：{aid}")
        if agent.owner_user_id != owner_user_id:
            raise BusinessError(f"Agent 不属于当前用户：{aid}")
        agents.append(agent)

    if replace:
        methodology.agents = agents
    else:
        existing = {a.id for a in methodology.agents}
        for agent in agents:
            if agent.id not in existing:
                methodology.agents.append(agent)

    if bump_version:
        bump_methodology(db, methodology)
    else:
        db.flush()
    return get_methodology(db, methodology_id, owner_user_id=owner_user_id)  # type: ignore[return-value]


def publish_methodology(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> Methodology:
    """发布：勾选的 Agent 中须至少有一个 Supervisor。"""
    row = get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    has_supervisor = any(
        (a.config or {}).get("role") == "supervisor" for a in row.agents
    )
    if not has_supervisor:
        raise BusinessError("发布失败：请先勾选至少一个 Supervisor Agent")
    row.status = "published"
    row.updated_time = datetime.now(timezone.utc)
    schedule_cache_invalidation(db, methodology_id)
    db.flush()
    snapshot_methodology(db, methodology_id)
    return row


def get_methodology_versions(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> list[dict]:
    if get_methodology(db, methodology_id, owner_user_id=owner_user_id) is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    return [
        {
            "methodology_id": r.methodology_id,
            "version": r.version,
            "created_time": r.created_time.isoformat(),
        }
        for r in list_revisions(db, methodology_id)
    ]


def _slug_id(name: str) -> str:
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    base = base.strip("_")[:48] or "methodology"
    return f"{base}_{uuid.uuid4().hex[:8]}"
