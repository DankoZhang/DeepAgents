"""
Skill 配置 API
==============

CRUD：前端可新增 / 编辑 / 删除 Skill（完整 SKILL.md 存库）。
运行时由 Factory 物化到 workspace 后交给 deepagents SkillsMiddleware。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from deepagents_app.api.deps import require_user
from deepagents_app.api.pagination import (
    limit_query,
    offset_query,
    set_total_count,
)
from deepagents_app.api.schemas import SkillCreate, SkillOut, SkillUpdate
from deepagents_app.db.session import get_db
from deepagents_app.services import skills as skills_svc

router = APIRouter(tags=["skills"])


@router.get("/skill/list", response_model=list[SkillOut])
def list_skills(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    rows, total = skills_svc.list_skills(
        db,
        owner_user_id=user_id,
        status=status,
        limit=limit,
        offset=offset,
    )
    set_total_count(response, total)
    return rows


@router.get("/skill/{skill_id}", response_model=SkillOut)
def get_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    row = skills_svc.get_skill(db, skill_id, owner_user_id=user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Skill 不存在")
    return row


@router.post("/skill", response_model=SkillOut)
def create_skill(
    body: SkillCreate,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return skills_svc.create_skill(
        db,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        content=body.content,
        config=body.config,
        status=body.status,
        skill_id=body.id,
    )


@router.patch("/skill/{skill_id}", response_model=SkillOut)
def update_skill(
    skill_id: str,
    body: SkillUpdate,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    return skills_svc.update_skill(
        db,
        skill_id,
        owner_user_id=user_id,
        name=body.name,
        description=body.description,
        content=body.content,
        config=body.config,
        status=body.status,
    )


@router.delete("/skill/{skill_id}")
def delete_skill(
    skill_id: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(require_user),
):
    skills_svc.delete_skill(db, skill_id, owner_user_id=user_id)
    return {"ok": True}
