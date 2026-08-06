"""
会话 API
========

会话创建时锁定方法论 version；后续聊天始终按该版本重建 Agent。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

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
def create_conversation(body: ConversationCreate, db: Session = Depends(get_db)):
    """仅允许对已发布方法论建会话；写入当前 methodology_version。"""
    return conversation_svc.create_conversation(
        db,
        methodology_id=body.methodology_id,
        user_id=body.user_id,
        thread_id=body.thread_id,
    )


@router.get("/conversation/list", response_model=list[ConversationOut])
def list_conversations(
    user_id: str | None = Query(None),
    methodology_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    return conversation_svc.list_conversations(
        db,
        user_id=user_id,
        methodology_id=methodology_id,
        limit=limit,
    )


@router.get("/conversation/{thread_id}", response_model=ConversationOut)
def get_conversation(thread_id: str, db: Session = Depends(get_db)):
    row = conversation_svc.get_conversation_by_thread(db, thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.delete("/conversation/{thread_id}")
def delete_conversation(thread_id: str, db: Session = Depends(get_db)):
    """仅删 Conversation 行；checkpointer 中的 thread 状态需另行清理。"""
    conversation_svc.delete_conversation(db, thread_id)
    return {"ok": True}


@router.get(
    "/conversation/{thread_id}/messages",
    response_model=ConversationMessagesOut,
)
def get_conversation_messages(thread_id: str, db: Session = Depends(get_db)):
    """读取会话历史消息（checkpointer state）。"""
    try:
        return ConversationMessagesOut(
            **load_conversation_messages(db, thread_id=thread_id)
        )
    except LookupError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
