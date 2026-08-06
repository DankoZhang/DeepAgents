"""
方法论 API
==========

CRUD + 发布 + 勾选全局 Agent + 版本列表。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from deepagents_app.api.deps import require_user
from deepagents_app.api.schemas import (
    MethodologyBindAgents,
    MethodologyCreate,
    MethodologyDetailOut,
    MethodologyOut,
    MethodologyUpdate,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import methodology as methodology_svc

router = APIRouter(tags=["methodology"])


@router.post("/methodology", response_model=MethodologyDetailOut)
def create_methodology(
    body: MethodologyCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """创建草稿方法论，可选 ``agent_ids`` 立即勾选全局 Agent。"""
    return methodology_svc.create_methodology(
        db,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        methodology_id=body.id,
        agent_ids=body.agent_ids or None,
    )


@router.get("/methodology/list", response_model=list[MethodologyOut])
def list_methodologies(
    status: str | None = Query(None, description="draft | published | archived"),
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return methodology_svc.list_methodologies(db, owner_user_id=user_id, status=status)


@router.get("/methodology/{methodology_id}", response_model=MethodologyDetailOut)
def get_methodology(
    methodology_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    row = methodology_svc.get_methodology(
        db, methodology_id, owner_user_id=user_id
    )
    if row is None:
        raise HTTPException(status_code=404, detail="方法论不存在")
    return row


@router.patch("/methodology/{methodology_id}", response_model=MethodologyOut)
def update_methodology(
    methodology_id: str,
    body: MethodologyUpdate,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return methodology_svc.update_methodology(
        db,
        methodology_id,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
    )


@router.delete("/methodology/{methodology_id}")
def delete_methodology(
    methodology_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    methodology_svc.delete_methodology(
        db, methodology_id, owner_user_id=user_id
    )
    return {"ok": True}


@router.post(
    "/methodology/{methodology_id}/agents",
    response_model=MethodologyDetailOut,
)
def bind_agents(
    methodology_id: str,
    body: MethodologyBindAgents,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    """勾选全局 Agent；``replace=True`` 时先清空再绑定。"""
    return methodology_svc.bind_methodology_agents(
        db,
        methodology_id,
        body.agent_ids,
        owner_user_id=user_id,
        replace=body.replace,
    )


@router.post("/methodology/{methodology_id}/publish", response_model=MethodologyOut)
def publish_methodology(
    methodology_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return methodology_svc.publish_methodology(
        db, methodology_id, owner_user_id=user_id
    )


@router.get("/methodology/{methodology_id}/versions")
def list_versions(
    methodology_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return methodology_svc.get_methodology_versions(
        db, methodology_id, owner_user_id=user_id
    )
