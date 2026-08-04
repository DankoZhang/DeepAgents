"""方法论版本快照：旧会话可按创建时版本重建 Agent。"""

# 推迟注解求值
from __future__ import annotations

# 快照写入时间
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Methodology, MethodologyRevision
from deepagents_app.services.llm_models import serialize_model_for_snapshot


def serialize_methodology(db: Session, methodology_id: str) -> dict[str, Any]:
    """把当前方法论完整配置序列化为可重建的 JSON。"""
    methodology = (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

    agents_payload: list[dict[str, Any]] = []
    for agent in methodology.agents:
        # 存可序列化字段；关系只存 id 列表，重建时再 load
        agents_payload.append(
            {
                "id": agent.id,
                "name": agent.name,
                "system_prompt": agent.system_prompt,
                "model_id": agent.model_id,
                # 钉死当时超参数，旧会话不随目录后续修改漂移
                "llm": serialize_model_for_snapshot(agent.llm_model),
                "config": dict(agent.config or {}),
                "tool_ids": [t.id for t in agent.tools],
                "middleware_ids": [m.id for m in agent.middlewares],
                "skill_ids": [s.id for s in agent.skills],
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

    payload = serialize_methodology(db, methodology_id)  # 生成 JSON
    # 同 methodology + version 已有则覆盖（幂等）
    existing = (
        db.query(MethodologyRevision)
        .filter(
            MethodologyRevision.methodology_id == methodology_id,
            MethodologyRevision.version == methodology.version,
        )
        .one_or_none()
    )
    if existing is not None:
        existing.snapshot = payload  # 覆盖内容
        existing.created_time = datetime.now(timezone.utc)
        db.flush()
        return existing

    # 新建 revision 行
    row = MethodologyRevision(
        id=f"{methodology_id}_v{methodology.version}",  # 可读主键
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
    # Factory 在会话锁定旧 version 时按此取快照
    return (
        db.query(MethodologyRevision)
        .filter(
            MethodologyRevision.methodology_id == methodology_id,
            MethodologyRevision.version == version,
        )
        .one_or_none()
    )


def list_revisions(db: Session, methodology_id: str) -> list[MethodologyRevision]:
    # 版本列表 API：新版本在前
    return (
        db.query(MethodologyRevision)
        .filter(MethodologyRevision.methodology_id == methodology_id)
        .order_by(MethodologyRevision.version.desc())
        .all()
    )


def bump_methodology(db: Session, methodology: Methodology) -> None:
    """配置变更收尾：升版 → 失效缓存 → 写当前 version 的快照。"""
    # 延迟导入，避免与 agent_factory ↔ revisions 形成环
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    methodology.version += 1
    methodology.updated_time = datetime.now(timezone.utc)
    invalidate_agent_cache(methodology.id)
    db.flush()
    snapshot_methodology(db, methodology.id)


def bump_methodologies_using_agent(db: Session, agent_id: str) -> None:
    """找出勾选了该全局 Agent 的全部方法论，逐个升版并快照。"""
    from deepagents_app.db.models import MethodologyAgent

    links = (
        db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id == agent_id)
        .all()
    )
    for link in links:
        methodology = db.get(Methodology, link.methodology_id)
        if methodology is not None:
            bump_methodology(db, methodology)


def bump_methodologies_using_model(db: Session, model_id: str) -> None:
    """模型超参数变更：bump 所有引用该模型的 Agent 所在方法论。"""
    from deepagents_app.db.models import MethodologyAgent
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    agent_ids = [
        a.id
        for a in db.query(AgentDefinition).filter(AgentDefinition.model_id == model_id).all()
    ]
    if not agent_ids:
        invalidate_agent_cache()
        return

    meth_ids = {
        link.methodology_id
        for link in db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id.in_(agent_ids))
        .all()
    }
    for mid in meth_ids:
        methodology = db.get(Methodology, mid)
        if methodology is not None:
            bump_methodology(db, methodology)
