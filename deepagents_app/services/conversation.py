"""
会话服务
========

创建 Conversation 时锁定方法论 version；后续聊天始终按该版本重建 Agent，
不受 live 表后续编辑影响。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.db.models import Conversation, Methodology
from deepagents_app.ownership import validate_thread_id


def create_conversation(
    db: Session,
    *,
    user_id: str,
    methodology_id: str,
    thread_id: str | None = None,
) -> Conversation:
    methodology = db.get(Methodology, methodology_id)
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    if methodology.owner_user_id != user_id:
        raise BusinessError(f"方法论不属于当前用户：{methodology_id}")
    if methodology.status != "published":
        raise BusinessError(
            f"方法论未发布（status={methodology.status}），请先调用 /publish"
        )

    if thread_id is not None:
        validate_thread_id(thread_id)
    tid = thread_id or uuid.uuid4().hex
    row = Conversation(
        id=uuid.uuid4().hex,
        thread_id=tid,
        user_id=user_id,
        methodology_id=methodology.id,
        methodology_version=methodology.version,
    )
    db.add(row)
    db.flush()
    return row


def get_conversation_by_thread(
    db: Session, thread_id: str, *, user_id: str
) -> Conversation | None:
    return (
        db.query(Conversation)
        .filter(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user_id,
        )
        .one_or_none()
    )


def list_conversations(
    db: Session,
    *,
    user_id: str,
    methodology_id: str | None = None,
    limit: int = 50,
) -> list[Conversation]:
    q = (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(Conversation.created_time.desc())
    )
    if methodology_id:
        q = q.filter(Conversation.methodology_id == methodology_id)
    return q.limit(limit).all()


def delete_conversation(db: Session, thread_id: str, *, user_id: str) -> None:
    row = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if row is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")
    db.delete(row)
    db.flush()
