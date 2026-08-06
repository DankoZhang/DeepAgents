"""
会话服务
========

创建 Conversation 时锁定方法论 version；后续聊天始终按该版本重建 Agent，
不受 live 表后续编辑影响。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import get_settings
from deepagents_app.db.models import Conversation, Methodology
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id, validate_thread_id

logger = logging.getLogger(__name__)


def create_conversation(
    db: Session,
    *,
    user_id: str,
    methodology_id: str,
    thread_id: str | None = None,
) -> Conversation:
    """
    创建会话并锁定当前方法论 version。

    后续 chat 一律按 ``methodology_version`` 重建 Agent，与 live 表解耦。
    """
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
        if (
            db.query(Conversation)
            .filter(Conversation.thread_id == thread_id)
            .one_or_none()
            is not None
        ):
            raise BusinessError(f"thread_id 已存在：{thread_id}")
    tid = thread_id or uuid.uuid4().hex
    row = Conversation(
        id=uuid.uuid4().hex,
        thread_id=tid,
        user_id=user_id,
        methodology_id=methodology.id,
        # 钉死版本：旧会话不跟随方法论后续升版
        methodology_version=methodology.version,
    )
    db.add(row)
    db.flush()
    return row


def get_conversation_by_thread(
    db: Session, thread_id: str, *, user_id: str
) -> Conversation | None:
    """按对外 thread_id + 所有者取会话（跨用户同 thread_id 互不可见）。"""
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
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[Conversation], int]:
    """列出当前用户会话；可按方法论过滤。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(Conversation)
        .filter(Conversation.user_id == user_id)
        .order_by(Conversation.created_time.desc())
    )
    if methodology_id:
        q = q.filter(Conversation.methodology_id == methodology_id)
    return paginate_query(q, limit=limit, offset=offset)


def delete_conversation(db: Session, thread_id: str, *, user_id: str) -> None:
    """删除会话元数据，并清理 checkpointer 中对应 thread 状态。"""
    row = get_conversation_by_thread(db, thread_id, user_id=user_id)
    if row is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")
    cp_thread = checkpoint_thread_id(user_id, thread_id)
    db.delete(row)
    db.flush()
    try:
        checkpointer = build_checkpointer(get_settings())
        delete_thread = getattr(checkpointer, "delete_thread", None)
        if callable(delete_thread):
            delete_thread(cp_thread)
        else:
            logger.warning(
                "checkpointer 无 delete_thread，跳过清理 thread=%s", cp_thread
            )
    except Exception as exc:  # noqa: BLE001
        # 元数据已删；checkpointer 残留不影响正确性，只影响磁盘占用
        logger.warning("清理 checkpointer 失败 thread=%s: %s", cp_thread, exc)
