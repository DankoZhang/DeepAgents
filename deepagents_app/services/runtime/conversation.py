"""
会话服务
========

创建 Conversation 时锁定方法论 version；后续聊天始终按该版本重建 Agent，
不受 live 表后续编辑影响。
"""

from __future__ import annotations

import logging
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import get_settings
from deepagents_app.db.models import Conversation, Methodology
from deepagents_app.db.pagination import DEFAULT_LIMIT, coerce_datetime, page_rows
from deepagents_app.factory import build_checkpointer
from deepagents_app.ownership import checkpoint_thread_id, validate_thread_id

logger = logging.getLogger(__name__)


async def create_conversation(
    db: AsyncSession,
    *,
    user_id: str,
    methodology_id: str,
    thread_id: str | None = None,
) -> Conversation:
    """
    创建会话并锁定当前方法论 version。

    后续 chat 一律按 ``methodology_version`` 重建 Agent，与 live 表解耦。
    """
    methodology = await db.get(Methodology, methodology_id)
    if methodology is None or methodology.owner_user_id != user_id:
        # 统一 404，避免泄露他人方法论是否存在
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    if methodology.status != "published":
        raise BusinessError(
            f"方法论未发布（status={methodology.status}），请先调用 /publish"
        )

    if thread_id is not None:
        validate_thread_id(thread_id)
        existing = (
            await db.scalars(
                select(Conversation).where(
                    Conversation.user_id == user_id,
                    Conversation.thread_id == thread_id,
                )
            )
        ).one_or_none()
        if existing is not None:
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
    await db.flush()
    return row


async def get_conversation_by_thread(
    db: AsyncSession, thread_id: str, *, user_id: str
) -> Conversation | None:
    """按对外 thread_id + 所有者取会话（跨用户同 thread_id 互不可见）。"""
    return (
        await db.scalars(
            select(Conversation).where(
                Conversation.thread_id == thread_id,
                Conversation.user_id == user_id,
            )
        )
    ).one_or_none()


async def list_conversations(
    db: AsyncSession,
    *,
    user_id: str,
    methodology_id: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[Conversation], int, str | None]:
    """列出当前用户会话；可按方法论过滤。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(Conversation)
        .where(Conversation.user_id == user_id)
        .order_by(Conversation.created_time.desc(), Conversation.id.desc())
    )
    if methodology_id:
        stmt = stmt.where(Conversation.methodology_id == methodology_id)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        cursor=cursor,
        sort_column=Conversation.created_time,
        id_column=Conversation.id,
        sort_attr="created_time",
        descending=True,
        coerce_sort=coerce_datetime,
    )


async def delete_conversation(db: AsyncSession, thread_id: str, *, user_id: str) -> None:
    """
    删除会话元数据，并清理 checkpointer 中对应 thread 状态。

    先 ``commit`` 元数据删除，再删 checkpoint：避免 commit 失败回滚后
    会话行恢复、历史却已永久丢失。checkpoint 清理失败只打日志（残留可接受）。
    """
    row = await get_conversation_by_thread(db, thread_id, user_id=user_id)
    if row is None:
        raise NotFoundError(f"会话不存在：thread_id={thread_id}")
    cp_thread = checkpoint_thread_id(user_id, thread_id)
    await db.delete(row)
    await db.commit()
    try:
        checkpointer = build_checkpointer(get_settings())
        await checkpointer.adelete_thread(cp_thread)
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理 checkpointer 失败 thread=%s: %s", cp_thread, exc)
