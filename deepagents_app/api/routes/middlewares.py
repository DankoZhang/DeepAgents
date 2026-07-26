"""
Middleware API（只读）
=====================

内置中间件由种子写入；前端仅可列表勾选，不可新建/编辑/删除。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import MiddlewareOut
from deepagents_app.db.session import get_db
from deepagents_app.services import middlewares as mw_svc

router = APIRouter(tags=["middlewares"])


@router.get("/middleware/list", response_model=list[MiddlewareOut])
def list_middlewares(db: Session = Depends(get_db)):
    return mw_svc.list_middlewares(db)


@router.get("/middleware/{middleware_id}", response_model=MiddlewareOut)
def get_middleware(middleware_id: str, db: Session = Depends(get_db)):
    row = mw_svc.get_middleware(db, middleware_id)
    if row is None:
        raise HTTPException(status_code=404, detail="中间件不存在")
    return row
