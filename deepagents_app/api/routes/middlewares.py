"""
Middleware API（只读）
=====================

内置中间件由用户 bootstrap 写入；前端仅可列表勾选，不可新建/编辑/删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from deepagents_app.api.deps import require_user
from deepagents_app.api.pagination import (
    limit_query,
    offset_query,
    set_total_count,
)
from deepagents_app.api.schemas import MiddlewareOut
from deepagents_app.db.session import get_db
from deepagents_app.services import middlewares as mw_svc

router = APIRouter(tags=["middlewares"])


@router.get("/middleware/list", response_model=list[MiddlewareOut])
def list_middlewares(
    response: Response,
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    rows, total = mw_svc.list_middlewares(
        db, owner_user_id=user_id, limit=limit, offset=offset
    )
    set_total_count(response, total)
    return rows


@router.get("/middleware/{middleware_id}", response_model=MiddlewareOut)
def get_middleware(
    middleware_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    row = mw_svc.get_middleware(db, middleware_id, owner_user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="中间件不存在")
    return row
