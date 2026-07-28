"""
聊天 API
========

- ``POST /chat``：按 thread 发一轮消息
- ``POST /chat/resume``：HITL 中断后批准 / 拒绝工具调用
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI 路由基础设施
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

# 聊天请求/响应模型
from deepagents_app.api.schemas import ChatRequest, ChatResponse, ChatResumeRequest
from deepagents_app.db.session import get_db
# 业务：一轮对话 invoke（内部会按会话锁定的 methodology_version 取 Agent）
from deepagents_app.services.chat import chat as run_chat
# 业务：HITL 恢复
from deepagents_app.services.chat import resume_chat

# OpenAPI 分组
router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)  # 发用户消息
def chat(body: ChatRequest, db: Session = Depends(get_db)):
    """按会话绑定的方法论版本 invoke Agent。"""
    try:
        # 返回 dict：reply / interrupted / methodology_* 等
        result = run_chat(db, thread_id=body.thread_id, message=body.message)
        # 解包为严格类型的响应模型
        return ChatResponse(**result)
    except LookupError as exc:
        # thread 对应会话不存在 → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # 模型/工具/图执行等未预期错误 → 500
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/chat/resume", response_model=ChatResponse)  # 继续被 HITL 打断的图
def chat_resume(body: ChatResumeRequest, db: Session = Depends(get_db)):
    """Human-in-the-loop：批准或拒绝中断后的工具调用。"""
    try:
        result = resume_chat(
            db, thread_id=body.thread_id, approve=body.approve  # True 批准 / False 拒绝
        )
        return ChatResponse(**result)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc)) from exc
