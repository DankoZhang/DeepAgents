"""
方法论 CRUD、发布、勾选全局 Agent
================================

版本语义：
- 草稿态改 Agent 勾选 / 被引用 Agent 变更 → 不升版，覆盖当前 version 快照
- 已发布方法论的配置变更 → 升版并写新快照，并按保留策略裁剪历史
- 仅改名称/描述 → 不升版
- 创建时先绑 Agent（不升版），再统一打 v1 快照
- 发布时钉死当前 version 快照（不额外升版）
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Conversation, Methodology
from deepagents_app.ownership import validate_resource_id
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
    # 服务层默认与 API 一致
limit: int = 200,
    offset: int = 0,
) -> tuple[list[Methodology], int]:
    """按所有者列出方法论；可按 draft/published 过滤。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(Methodology)
        .filter(Methodology.owner_user_id == owner_user_id)
        .order_by(Methodology.updated_time.desc())
    )
    if status:
        q = q.filter(Methodology.status == status)
    return paginate_query(q, limit=limit, offset=offset)


def get_methodology(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> Methodology | None:
    """取单个方法论（含已勾选 Agent 及其 tools/middlewares/skills/llm）。"""
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
    mid = validate_resource_id(mid, label="methodology id")
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
    # 创建阶段不升版：后面统一 snapshot 成 v1，避免绑 Agent 时 version 变成 2
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
    """删除方法论；仍有会话引用时拒绝，避免孤儿 Conversation。"""
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
    """
    方法论勾选 / 替换全局 Agent 列表。

    ``bump_version=True``（默认）：视为配置变更 →
    draft 覆盖当前快照；published 升版 + 快照 + 失效缓存。
    ``bump_version=False``：仅改关联，留给调用方自行 snapshot（如创建流程）。
    """
    methodology = get_methodology(db, methodology_id, owner_user_id=owner_user_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")

    # 只能勾选当前用户自己的全局 Agent
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
        # 增量追加：已有的不重复加入
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
    """发布：勾选的 Agent 中须至少有一个 Supervisor；发布后才能建会话。"""
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
    # 发布瞬间再钉一版快照，保证旧会话可按该 version 重建
    snapshot_methodology(db, methodology_id)
    return row


def get_methodology_versions(
    db: Session, methodology_id: str, *, owner_user_id: str
) -> list[dict]:
    """列出该方法论历史快照版本（供前端/调试查看）。"""
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
    """由名称生成可读主键：清洗后截断 + 短 uuid 后缀防撞。"""
    base = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip())
    base = base.strip("_")[:48] or "methodology"
    return f"{base}_{uuid.uuid4().hex[:8]}"
