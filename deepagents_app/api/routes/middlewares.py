"""
Middleware 注册 API
===================

元信息 CRUD；运行时按 class_path 实例化并挂到 Agent。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import (
    MiddlewareCreate,
    MiddlewareOut,
    MiddlewareUpdate,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import middlewares as mw_svc

router = APIRouter(tags=["middlewares"])


@router.get("/middleware/list", response_model=list[MiddlewareOut])
def list_middlewares(db: Session = Depends(get_db)):
    return mw_svc.list_middlewares(db)


@router.post("/middleware", response_model=MiddlewareOut)
def create_middleware(body: MiddlewareCreate, db: Session = Depends(get_db)):
    """``class_path`` 指向 Middleware 类，config 作为构造参数。"""
    try:
        return mw_svc.create_middleware(
            db,
            name=body.name,
            class_path=body.class_path,
            config=body.config,
            middleware_id=body.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/middleware/{middleware_id}", response_model=MiddlewareOut)
def get_middleware(middleware_id: str, db: Session = Depends(get_db)):
    row = mw_svc.get_middleware(db, middleware_id)
    if row is None:
        raise HTTPException(status_code=404, detail="中间件不存在")
    return row


@router.patch("/middleware/{middleware_id}", response_model=MiddlewareOut)
def update_middleware(
    middleware_id: str,
    body: MiddlewareUpdate,
    db: Session = Depends(get_db),
):
    try:
        return mw_svc.update_middleware(
            db,
            middleware_id,
            name=body.name,
            class_path=body.class_path,
            config=body.config,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/middleware/{middleware_id}")
def delete_middleware(middleware_id: str, db: Session = Depends(get_db)):
    try:
        mw_svc.delete_middleware(db, middleware_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
