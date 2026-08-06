"""
聊天 API
========

- ``POST /chat``：按 thread 发一轮消息
- ``POST /chat/resume``：HITL 中断后批准 / 拒绝工具调用
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from deepagents_app.api.deps import require_user
from deepagents_app.api.schemas import ChatRequest, ChatResponse, ChatResumeRequest
from deepagents_app.db.session import get_db
from deepagents_app.services.chat import chat as run_chat
from deepagents_app.services.chat import resume_chat

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """按会话绑定的方法论版本 invoke Agent。"""
    return ChatResponse(
        **run_chat(
            db,
            user_id=user_id,
            thread_id=body.thread_id,
            message=body.message,
        )
    )


@router.post("/chat/resume", response_model=ChatResponse)
def chat_resume(
    body: ChatResumeRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """Human-in-the-loop：批准或拒绝中断后的工具调用。"""
    return ChatResponse(
        **resume_chat(
            db,
            user_id=user_id,
            thread_id=body.thread_id,
            approve=body.approve,
        )
    )
