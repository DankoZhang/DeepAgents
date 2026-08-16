#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   skills.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   skills.py

Skill 配置 API
==============

CRUD：前端可新增 / 编辑 / 删除 Skill（完整 SKILL.md 存库）。
上传：``POST /api/skill/upload`` 提交技能目录 zip（附属文件写入 files）。
运行时由 Factory 物化到 workspace 后交给 deepagents SkillsMiddleware。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Query, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import SkillCreate, SkillOut, SkillUpdate
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import skills as skills_svc
from deepagents_app.utils.skill_package import load_skill_package_from_bytes

router = APIRouter(tags=["skills"])


def _form_optional(value: str | None) -> str | None:
    """空表单字段视为未传，避免覆盖包内 name/description。"""
    if value is None:
        return None
    text = value.strip()
    return text or None


@router.get("/skill/list", response_model=list[SkillOut])
async def list_skills(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    limit: int = Depends(limit_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await skills_svc.list_skills(
        db,
        owner_user_id=user_id,
        status=status,
        limit=limit,
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


@router.post("/skill/upload", response_model=SkillOut)
async def upload_skill(
    file: UploadFile = File(..., description="技能目录 zip（含 SKILL.md）"),
    name: str | None = Form(None),
    description: str | None = Form(None),
    status: str = Form("active"),
    id: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """上传完整技能目录包；附属文件写入 files，不长期保存 zip。"""
    package = load_skill_package_from_bytes(
        await file.read(), filename=file.filename
    )
    return await skills_svc.create_skill_from_package(
        db,
        package,
        owner_user_id=user_id,
        name_override=_form_optional(name),
        description_override=_form_optional(description),
        status=status,
        skill_id=_form_optional(id),
    )


@router.post("/skill/{skill_id}/upload", response_model=SkillOut)
async def replace_skill_package(
    skill_id: str,
    file: UploadFile = File(..., description="技能目录 zip（含 SKILL.md）"),
    name: str | None = Form(None),
    description: str | None = Form(None),
    status: str | None = Form(None),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """用目录包整包替换已有 Skill（content + files）。"""
    package = load_skill_package_from_bytes(
        await file.read(), filename=file.filename
    )
    return await skills_svc.replace_skill_from_package(
        db,
        skill_id,
        package,
        owner_user_id=user_id,
        name_override=_form_optional(name),
        description_override=_form_optional(description),
        status=_form_optional(status),
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
