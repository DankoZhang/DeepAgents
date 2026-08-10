"""
Skill 配置 API
==============

CRUD：前端可新增 / 编辑 / 删除 Skill（完整 SKILL.md 存库）。
运行时由 Factory 物化到 workspace 后交给 deepagents SkillsMiddleware。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    offset_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import SkillCreate, SkillOut, SkillUpdate
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import skills as skills_svc
router = APIRouter(tags=["skills"])


@router.get("/skill/list", response_model=list[SkillOut])
async def list_skills(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await skills_svc.list_skills(
        db,
        owner_user_id=user_id,
        status=status,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.get("/skill/{skill_id}", response_model=SkillOut)
async def get_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await skills_svc.get_skill(db, skill_id, owner_user_id=user_id)
    return require_entity(row, "Skill 不存在")


@router.post("/skill", response_model=SkillOut)
async def create_skill(
    body: SkillCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await skills_svc.create_skill(
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
async def update_skill(
    skill_id: str,
    body: SkillUpdate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await skills_svc.update_skill(
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
async def delete_skill(
    skill_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    await skills_svc.delete_skill(db, skill_id, owner_user_id=user_id)
    return {"ok": True}
