"""
会话服务
========

创建 Conversation 时锁定方法论 version；后续聊天始终按该版本重建 Agent，
不受 live 表后续编辑影响。
"""

# 推迟注解求值
from __future__ import annotations

# 生成会话 id / 默认 thread_id
import uuid

# ORM 会话
from sqlalchemy.orm import Session

# Conversation：会话登记表；Methodology：校验是否已发布
from deepagents_app.db.models import Conversation, Methodology


def create_conversation(
    db: Session,
    *,
    methodology_id: str,  # 必须是已发布的方法论
    user_id: str | None = None,  # 可选用户标识
    thread_id: str | None = None,  # 可选；不传则自动生成
) -> Conversation:
    # 校验方法论存在
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    # 新会话仅允许已发布方法论（旧会话仍按创建时 version 运行）
    if methodology.status != "published":
        raise ValueError(
            f"方法论未发布（status={methodology.status}），请先调用 /publish"
        )

    # LangGraph / Redis checkpointer 用的隔离键
    tid = thread_id or uuid.uuid4().hex
    row = Conversation(
        id=uuid.uuid4().hex,  # 业务主键（与 thread_id 可不同）
        thread_id=tid,  # 多轮状态键
        user_id=user_id,
        methodology_id=methodology.id,
        # 锁定此刻的版本号；Agent Factory 据此选择 live 或 snapshot
        methodology_version=methodology.version,
    )
    db.add(row)  # pending insert
    db.flush()  # 立即校验唯一约束等
    return row


def get_conversation_by_thread(db: Session, thread_id: str) -> Conversation | None:
    # chat / messages 都用 thread_id 定位会话登记行
    return (
        db.query(Conversation)
        .filter(Conversation.thread_id == thread_id)
        .one_or_none()  # 0 或 1；thread_id 有 unique
    )


def list_conversations(
    db: Session,
    *,
    user_id: str | None = None,  # 可选按用户过滤
    methodology_id: str | None = None,  # 可选按方法论过滤
    limit: int = 50,  # 返回条数上限
) -> list[Conversation]:
    # 新会话在前
    q = db.query(Conversation).order_by(Conversation.created_time.desc())
    if user_id:
        q = q.filter(Conversation.user_id == user_id)
    if methodology_id:
        q = q.filter(Conversation.methodology_id == methodology_id)
    return q.limit(limit).all()


def delete_conversation(db: Session, thread_id: str) -> None:
    # 只删 Postgres 登记；Redis checkpoint 需另行清理
    row = get_conversation_by_thread(db, thread_id)
    if row is None:
        raise LookupError(f"会话不存在：thread_id={thread_id}")
    db.delete(row)
    db.flush()
