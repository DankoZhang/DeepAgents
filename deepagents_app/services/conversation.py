"""
会话服务
========

创建 Conversation 时锁定方法论 version；后续聊天始终按该版本重建 Agent，
不受 live 表后续编辑影响。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from deepagents_app.db.models import Conversation, Methodology


def create_conversation(
    db: Session,
    *,
    methodology_id: str,
    user_id: str | None = None,
    thread_id: str | None = None,
) -> Conversation:
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    # 新会话仅允许已发布方法论（旧会话仍按创建时 version 运行）
    if methodology.status != "published":
        raise ValueError(
            f"方法论未发布（status={methodology.status}），请先调用 /publish"
        )

    tid = thread_id or uuid.uuid4().hex
    row = Conversation(
        id=uuid.uuid4().hex,
        thread_id=tid,
        user_id=user_id,
        methodology_id=methodology.id,
        # 锁定此刻的版本号；Agent Factory 据此选择 live 或 snapshot
        methodology_version=methodology.version,
    )
    db.add(row)
    db.flush()
    return row


def get_conversation_by_thread(db: Session, thread_id: str) -> Conversation | None:
    return (
        db.query(Conversation)
        .filter(Conversation.thread_id == thread_id)
        .one_or_none()
    )


def list_conversations(
    db: Session,
    *,
    user_id: str | None = None,
    methodology_id: str | None = None,
    limit: int = 50,
) -> list[Conversation]:
    q = db.query(Conversation).order_by(Conversation.created_time.desc())
    if user_id:
        q = q.filter(Conversation.user_id == user_id)
    if methodology_id:
        q = q.filter(Conversation.methodology_id == methodology_id)
    return q.limit(limit).all()


def delete_conversation(db: Session, thread_id: str) -> None:
    row = get_conversation_by_thread(db, thread_id)
    if row is None:
        raise LookupError(f"会话不存在：thread_id={thread_id}")
    db.delete(row)
    db.flush()
