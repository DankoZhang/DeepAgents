"""方法论 CRUD、发布、勾选全局 Agent。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from deepagents_app.db.models import AgentDefinition, Methodology, MethodologyAgent
from deepagents_app.services.agent_factory import invalidate_agent_cache
from deepagents_app.services.revisions import list_revisions, snapshot_methodology


def list_methodologies(
    db: Session,
    *,
    status: str | None = None,
) -> list[Methodology]:
    q = db.query(Methodology).order_by(Methodology.updated_time.desc())
    if status:
        q = q.filter(Methodology.status == status)
    return q.all()


def get_methodology(db: Session, methodology_id: str) -> Methodology | None:
    return (
        db.query(Methodology)
        .options(
            joinedload(Methodology.agents).joinedload(AgentDefinition.tools),
            joinedload(Methodology.agents).joinedload(AgentDefinition.middlewares),
        )
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )


def create_methodology(
    db: Session,
    *,
    name: str,
    description: str = "",
    methodology_id: str | None = None,
    agent_ids: list[str] | None = None,
) -> Methodology:
    """创建草稿方法论，可选立即勾选全局 Agent，并写入 v1 快照。"""
    mid = methodology_id or _slug_id(name)
    if db.get(Methodology, mid) is not None:
        raise ValueError(f"方法论已存在：{mid}")
    row = Methodology(
        id=mid,
        name=name,
        description=description,
        version=1,
        status="draft",
    )
    db.add(row)
    db.flush()
    if agent_ids:
        bind_methodology_agents(db, mid, agent_ids, replace=True, bump_version=False)
    snapshot_methodology(db, mid)
    return get_methodology(db, mid) or row


def update_methodology(
    db: Session,
    methodology_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    bump_version: bool = True,
) -> Methodology:
    row = db.get(Methodology, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if bump_version:
        row.version += 1
        invalidate_agent_cache(methodology_id)
    row.updated_time = datetime.now(timezone.utc)
    db.flush()
    if bump_version:
        snapshot_methodology(db, methodology_id)
    return row


def delete_methodology(db: Session, methodology_id: str) -> None:
    row = db.get(Methodology, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    invalidate_agent_cache(methodology_id)
    db.delete(row)
    db.flush()


def bind_methodology_agents(
    db: Session,
    methodology_id: str,
    agent_ids: list[str],
    *,
    replace: bool = True,
    bump_version: bool = True,
) -> Methodology:
    """方法论勾选 / 替换全局 Agent 列表。"""
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

    if replace:
        db.query(MethodologyAgent).filter(
            MethodologyAgent.methodology_id == methodology_id
        ).delete()

    for aid in agent_ids:
        agent = db.get(AgentDefinition, aid)
        if agent is None:
            raise LookupError(f"Agent 不存在：{aid}")
        exists = (
            db.query(MethodologyAgent)
            .filter(
                MethodologyAgent.methodology_id == methodology_id,
                MethodologyAgent.agent_id == aid,
            )
            .one_or_none()
        )
        if exists is None:
            db.add(MethodologyAgent(methodology_id=methodology_id, agent_id=aid))

    if bump_version:
        methodology.version += 1
        methodology.updated_time = datetime.now(timezone.utc)
        invalidate_agent_cache(methodology_id)
    db.flush()
    if bump_version:
        snapshot_methodology(db, methodology_id)
    return get_methodology(db, methodology_id)  # type: ignore[return-value]


def publish_methodology(db: Session, methodology_id: str) -> Methodology:
    """发布：勾选的 Agent 中须至少有一个 Supervisor。"""
    row = get_methodology(db, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    has_supervisor = any(
        (a.config or {}).get("role") == "supervisor" for a in row.agents
    )
    if not has_supervisor:
        raise ValueError("发布失败：请先勾选至少一个 Supervisor Agent")
    row.status = "published"
    row.updated_time = datetime.now(timezone.utc)
    invalidate_agent_cache(methodology_id)
    db.flush()
    snapshot_methodology(db, methodology_id)
    return row


def get_methodology_versions(db: Session, methodology_id: str) -> list[dict]:
    if db.get(Methodology, methodology_id) is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
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
