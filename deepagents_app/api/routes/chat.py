"""
聊天 API
========

- ``POST /chat``：按 thread 发一轮消息（sync，由 FastAPI 线程池执行）
- ``POST /chat/stream``：SSE 流式（token / done / error）
- ``POST /chat/resume`` / ``/chat/resume/stream``：HITL 批准/拒绝
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.schemas import ChatRequest, ChatResponse, ChatResumeRequest
from deepagents_app.db.session import get_db
from deepagents_app.services.chat import chat as run_chat
from deepagents_app.services.chat import iter_chat_sse, iter_resume_sse
from deepagents_app.services.chat import resume_chat

router = APIRouter(tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


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


@router.post("/chat/stream")
def chat_stream(
    body: ChatRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """SSE：``meta`` → ``token``* → ``done`` | ``error``。"""

    def event_gen():
        yield from iter_chat_sse(
            db,
            user_id=user_id,
            thread_id=body.thread_id,
            message=body.message,
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
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


@router.post("/chat/resume/stream")
def chat_resume_stream(
    body: ChatResumeRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """HITL 恢复的 SSE 版本。"""

    def event_gen():
        yield from iter_resume_sse(
            db,
            user_id=user_id,
            thread_id=body.thread_id,
            approve=body.approve,
        )

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
