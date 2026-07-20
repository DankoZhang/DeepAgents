"""
聊天 API
========

- ``POST /chat``：按 thread 发一轮消息
- ``POST /chat/resume``：HITL 中断后批准 / 拒绝工具调用
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import ChatRequest, ChatResponse, ChatResumeRequest
from deepagents_app.db.session import get_db
from deepagents_app.services.chat import chat as run_chat
from deepagents_app.services.chat import resume_chat

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest, db: Session = Depends(get_db)):
    """按会话绑定的方法论版本 invoke Agent。"""
    try:
        result = run_chat(db, thread_id=body.thread_id, message=body.message)
        return ChatResponse(**result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat/resume", response_model=ChatResponse)
def chat_resume(body: ChatResumeRequest, db: Session = Depends(get_db)):
    """Human-in-the-loop：批准或拒绝中断后的工具调用。"""
    try:
        result = resume_chat(
            db, thread_id=body.thread_id, approve=body.approve
        )
        return ChatResponse(**result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
