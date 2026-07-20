"""方法论 CRUD 与发布 / 版本管理。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session, joinedload

from deepagents_app.db.models import AgentDefinition, Methodology
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
) -> Methodology:
    """创建草稿方法论，并写入 v1 初始快照。"""
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
    snapshot_methodology(db, mid)
    return row


def update_methodology(
    db: Session,
    methodology_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    bump_version: bool = True,
) -> Methodology:
    """
    修改方法论元信息。

    默认 bump_version=True：配置变更 → version+1，快照并失效缓存。
    """
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


def publish_methodology(db: Session, methodology_id: str) -> Methodology:
    """
    发布方法论：校验存在 Supervisor → status=published → 快照。

    只有 published 状态才能创建新会话。
    """
    row = db.get(Methodology, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    agents = (
        db.query(AgentDefinition)
        .filter(AgentDefinition.methodology_id == methodology_id)
        .all()
    )
    has_supervisor = any(
        (a.config or {}).get("role") == "supervisor" for a in agents
    )
    if not has_supervisor:
        raise ValueError("发布失败：请先配置至少一个 Supervisor Agent")
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
