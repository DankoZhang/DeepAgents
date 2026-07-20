"""
方法论 API
==========

CRUD + 发布 + 版本列表。配置变更会 bump version 并写快照。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import (
    MethodologyCreate,
    MethodologyDetailOut,
    MethodologyOut,
    MethodologyUpdate,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import methodology as methodology_svc

router = APIRouter(tags=["methodology"])


@router.post("/methodology", response_model=MethodologyOut)
def create_methodology(body: MethodologyCreate, db: Session = Depends(get_db)):
    """创建草稿方法论（初始 version=1）。"""
    try:
        return methodology_svc.create_methodology(
            db,
            name=body.name,
            description=body.description,
            methodology_id=body.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/methodology/list", response_model=list[MethodologyOut])
def list_methodologies(
    status: str | None = Query(None, description="draft | published | archived"),
    db: Session = Depends(get_db),
):
    return methodology_svc.list_methodologies(db, status=status)


@router.get("/methodology/{methodology_id}", response_model=MethodologyDetailOut)
def get_methodology(methodology_id: str, db: Session = Depends(get_db)):
    """详情含 agents 及其绑定的 tools / middlewares。"""
    row = methodology_svc.get_methodology(db, methodology_id)
    if row is None:
        raise HTTPException(status_code=404, detail="方法论不存在")
    return row


@router.patch("/methodology/{methodology_id}", response_model=MethodologyOut)
def update_methodology(
    methodology_id: str,
    body: MethodologyUpdate,
    db: Session = Depends(get_db),
):
    try:
        return methodology_svc.update_methodology(
            db,
            methodology_id,
            name=body.name,
            description=body.description,
            bump_version=body.bump_version,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/methodology/{methodology_id}")
def delete_methodology(methodology_id: str, db: Session = Depends(get_db)):
    try:
        methodology_svc.delete_methodology(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/methodology/{methodology_id}/publish", response_model=MethodologyOut)
def publish_methodology(methodology_id: str, db: Session = Depends(get_db)):
    """发布前校验至少存在一个 Supervisor；发布后才可创建会话。"""
    try:
        return methodology_svc.publish_methodology(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/methodology/{methodology_id}/versions")
def list_versions(methodology_id: str, db: Session = Depends(get_db)):
    """列出历史快照版本（供排查旧会话用）。"""
    try:
        return methodology_svc.get_methodology_versions(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
