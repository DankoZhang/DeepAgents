"""
会话 API
========

会话创建时锁定方法论 version；后续聊天始终按该版本重建 Agent。
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI 路由基础设施
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

# 会话相关 schema
from deepagents_app.api.schemas import (
    ConversationCreate,  # 建会话 body
    ConversationMessagesOut,  # 历史消息响应
    ConversationOut,  # 会话元信息响应
)
from deepagents_app.db.session import get_db
# 会话 CRUD（Postgres Conversation 表）
from deepagents_app.services import conversation as conversation_svc
# 从 Redis checkpointer 读消息历史（与 CRUD 分离）
from deepagents_app.services.chat import get_conversation_messages as load_conversation_messages

# OpenAPI 分组
router = APIRouter(tags=["conversation"])


@router.post("/conversation", response_model=ConversationOut)  # 开新会话
def create_conversation(body: ConversationCreate, db: Session = Depends(get_db)):
    """仅允许对已发布方法论建会话；写入当前 methodology_version。"""
    try:
        return conversation_svc.create_conversation(
            db,
            methodology_id=body.methodology_id,  # 必须已 published
            user_id=body.user_id,  # 可选用户标识
            thread_id=body.thread_id,  # 可选；否则服务端生成
        )
    except LookupError as exc:
        # 方法论不存在 → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 未发布等业务错误 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/conversation/list", response_model=list[ConversationOut])  # 会话列表
def list_conversations(
    user_id: str | None = Query(None),  # 可选按用户过滤
    methodology_id: str | None = Query(None),  # 可选按方法论过滤
    limit: int = Query(50, ge=1, le=200),  # 分页上限保护
    db: Session = Depends(get_db),
):
    return conversation_svc.list_conversations(
        db,
        user_id=user_id,
        methodology_id=methodology_id,
        limit=limit,
    )


@router.get("/conversation/{thread_id}", response_model=ConversationOut)  # 会话元信息
def get_conversation(thread_id: str, db: Session = Depends(get_db)):
    # 按 LangGraph thread_id 查 Conversation 行（不含消息正文）
    row = conversation_svc.get_conversation_by_thread(db, thread_id)
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    return row


@router.delete("/conversation/{thread_id}")  # 删会话登记
def delete_conversation(thread_id: str, db: Session = Depends(get_db)):
    """仅删 Conversation 行；checkpointer 中的 thread 状态需另行清理。"""
    try:
        conversation_svc.delete_conversation(db, thread_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.get(
    "/conversation/{thread_id}/messages",  # 聊天页回放历史
    response_model=ConversationMessagesOut,
)
def get_conversation_messages(thread_id: str, db: Session = Depends(get_db)):
    """读取会话历史消息（checkpointer state）。"""
    try:
        # service 返回 dict，再校验/构造成响应模型
        return ConversationMessagesOut(**load_conversation_messages(db, thread_id=thread_id))
    except LookupError as exc:
        # 会话不存在
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        # checkpointer/序列化异常 → 500
        raise HTTPException(status_code=500, detail=str(exc)) from exc
