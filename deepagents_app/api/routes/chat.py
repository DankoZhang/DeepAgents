"""聊天 API。"""

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
