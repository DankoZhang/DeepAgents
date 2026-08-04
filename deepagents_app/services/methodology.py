"""方法论 CRUD、发布、勾选全局 Agent。"""

# 推迟注解求值
from __future__ import annotations

# 生成方法论 id 后缀
import uuid
# 更新 updated_time
from datetime import datetime, timezone

# Session + 预加载关系
from sqlalchemy.orm import Session

# Methodology / 勾选关系 / Agent（详情预加载）
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Methodology, MethodologyAgent
# 配置变更后清 Compiled Agent 缓存
from deepagents_app.services.agent_factory import invalidate_agent_cache
from deepagents_app.services.revisions import bump_methodology, list_revisions, snapshot_methodology


def list_methodologies(
    db: Session,
    *,
    status: str | None = None,  # draft | published | archived
) -> list[Methodology]:
    # 最近更新的在前
    q = db.query(Methodology).order_by(Methodology.updated_time.desc())
    if status:
        q = q.filter(Methodology.status == status)
    return q.all()


def get_methodology(db: Session, methodology_id: str) -> Methodology | None:
    # 详情需带出已勾选 Agent 及其 tools / middlewares / skills
    return (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )


def create_methodology(
    db: Session,
    *,
    name: str,
    description: str = "",
    methodology_id: str | None = None,  # 可选指定 id
    agent_ids: list[str] | None = None,  # 创建时可直接勾选
) -> Methodology:
    """创建草稿方法论，可选立即勾选全局 Agent，并写入 v1 快照。"""
    mid = methodology_id or _slug_id(name)  # 无指定则由名称生成
    if db.get(Methodology, mid) is not None:
        raise ValueError(f"方法论已存在：{mid}")
    row = Methodology(
        id=mid,
        name=name,
        description=description,
        version=1,  # 初始版本
        status="draft",  # 未发布不能建会话
    )
    db.add(row)
    db.flush()
    if agent_ids:
        # 创建路径不 bump（已是 v1），只写关系
        bind_methodology_agents(db, mid, agent_ids, replace=True, bump_version=False)
    snapshot_methodology(db, mid)  # 写入 v1 快照
    return get_methodology(db, mid) or row  # 优先返回带关系的详情


def update_methodology(
    db: Session,
    methodology_id: str,
    *,
    name: str | None = None,
    description: str | None = None,
    bump_version: bool = True,  # False：只改文字不升版
) -> Methodology:
    row = db.get(Methodology, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if bump_version:
        bump_methodology(db, row)
    else:
        row.updated_time = datetime.now(timezone.utc)
        db.flush()
    return row


def delete_methodology(db: Session, methodology_id: str) -> None:
    row = db.get(Methodology, methodology_id)
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    invalidate_agent_cache(methodology_id)  # 先清缓存
    db.delete(row)  # cascade 勾选关系与 revision；不删全局 Agent
    db.flush()


def bind_methodology_agents(
    db: Session,
    methodology_id: str,
    agent_ids: list[str],  # 要勾选的全局 Agent id
    *,
    replace: bool = True,  # True 先清空再绑
    bump_version: bool = True,  # 创建时传 False 避免二次升版
) -> Methodology:
    """方法论勾选 / 替换全局 Agent 列表。"""
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")

    if replace:
        # 删掉该方法论全部旧勾选
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
        bump_methodology(db, methodology)
    else:
        db.flush()
    return get_methodology(db, methodology_id)  # type: ignore[return-value]


def publish_methodology(db: Session, methodology_id: str) -> Methodology:
    """发布：勾选的 Agent 中须至少有一个 Supervisor。"""
    row = get_methodology(db, methodology_id)  # 带 agents 关系
    if row is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    # deepagents 需要主调度 Agent
    has_supervisor = any(
        (a.config or {}).get("role") == "supervisor" for a in row.agents
    )
    if not has_supervisor:
        raise ValueError("发布失败：请先勾选至少一个 Supervisor Agent")
    row.status = "published"  # 此后才允许 create_conversation
    row.updated_time = datetime.now(timezone.utc)
    invalidate_agent_cache(methodology_id)
    db.flush()
    snapshot_methodology(db, methodology_id)  # 发布点快照
    return row


def get_methodology_versions(db: Session, methodology_id: str) -> list[dict]:
    # 先确认方法论存在
    if db.get(Methodology, methodology_id) is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    # 转成前端友好的 dict 列表
    return [
        {
            "methodology_id": r.methodology_id,
            "version": r.version,
            "created_time": r.created_time.isoformat(),
        }
        for r in list_revisions(db, methodology_id)
    ]


def _slug_id(name: str) -> str:
    # 名称转安全 id 前缀 + 短随机后缀，避免冲突
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    base = base.strip("_")[:48] or "methodology"
    return f"{base}_{uuid.uuid4().hex[:8]}"
