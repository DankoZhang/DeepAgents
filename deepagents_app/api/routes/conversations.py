"""
会话 API
========

会话创建时锁定方法论 version；后续聊天始终按该版本重建 Agent。
所有操作按登录用户隔离。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from deepagents_app.api.deps import require_user
from deepagents_app.api.pagination import (
    limit_query,
    offset_query,
    set_total_count,
)
from deepagents_app.api.schemas import (
    ConversationCreate,
    ConversationMessagesOut,
    ConversationOut,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import conversation as conversation_svc
from deepagents_app.services.chat import get_conversation_messages as load_conversation_messages

router = APIRouter(tags=["conversation"])


@router.post("/conversation", response_model=ConversationOut)
def create_conversation(
    body: ConversationCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """仅允许对本用户已发布方法论建会话；写入当前 methodology_version。"""
    return conversation_svc.create_conversation(
        db,
        user_id=user_id,
        methodology_id=body.methodology_id,
        thread_id=body.thread_id,
    )


@router.get("/conversation/list", response_model=list[ConversationOut])
def list_conversations(
    response: Response,
    methodology_id: str | None = Query(None),
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    rows, total = conversation_svc.list_conversations(
        db,
        user_id=user_id,
        methodology_id=methodology_id,
        limit=limit,
        offset=offset,
    )
    set_total_count(response, total)
    return rows


@router.get("/conversation/{thread_id}", response_model=ConversationOut)
def get_conversation(
    thread_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    from fastapi import HTTPException

    row = conversation_svc.get_conversation_by_thread(
        db, thread_id, user_id=user_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.delete("/conversation/{thread_id}")
def delete_conversation(
    thread_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """仅删 Conversation 行，并尽量清理 checkpointer 中的 thread 状态。"""
    conversation_svc.delete_conversation(db, thread_id, user_id=user_id)
    return {"ok": True}


@router.get(
    "/conversation/{thread_id}/messages",
    response_model=ConversationMessagesOut,
)
def get_conversation_messages(
    thread_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """读取会话历史消息（checkpointer state，不编译 Agent）。"""
    return ConversationMessagesOut(
        **load_conversation_messages(db, user_id=user_id, thread_id=thread_id)
    )
