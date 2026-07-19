"""方法论版本快照：旧会话可按创建时版本重建 Agent。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session, joinedload

from deepagents_app.db.models import AgentDefinition, Methodology, MethodologyRevision


def serialize_methodology(db: Session, methodology_id: str) -> dict[str, Any]:
    """把当前方法论完整配置序列化为可重建的 JSON。"""
    methodology = (
        db.query(Methodology)
        .options(
            joinedload(Methodology.agents).joinedload(AgentDefinition.tools),
            joinedload(Methodology.agents).joinedload(AgentDefinition.middlewares),
        )
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

    agents_payload: list[dict[str, Any]] = []
    for agent in methodology.agents:
        agents_payload.append(
            {
                "id": agent.id,
                "name": agent.name,
                "system_prompt": agent.system_prompt,
                "model": agent.model,
                "temperature": agent.temperature,
                "config": dict(agent.config or {}),
                "tool_ids": [t.id for t in agent.tools],
                "middleware_ids": [m.id for m in agent.middlewares],
            }
        )
    return {
        "id": methodology.id,
        "name": methodology.name,
        "description": methodology.description,
        "version": methodology.version,
        "status": methodology.status,
        "agents": agents_payload,
    }


def snapshot_methodology(db: Session, methodology_id: str) -> MethodologyRevision:
    """按当前 version 写入/覆盖快照。"""
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

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
    return (
        db.query(MethodologyRevision)
        .filter(
            MethodologyRevision.methodology_id == methodology_id,
            MethodologyRevision.version == version,
        )
        .one_or_none()
    )


def list_revisions(db: Session, methodology_id: str) -> list[MethodologyRevision]:
    return (
        db.query(MethodologyRevision)
        .filter(MethodologyRevision.methodology_id == methodology_id)
        .order_by(MethodologyRevision.version.desc())
        .all()
    )
