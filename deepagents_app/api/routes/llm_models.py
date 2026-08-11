"""
大模型目录 API
==============

前端统一配置 provider / 模型名 / 超参数，并支持连通性测试。
Agent 通过 model_id 绑定目录中的模型。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import (
    ModelCreate,
    ModelOut,
    ModelTestRequest,
    ModelTestResult,
    ModelUpdate,
)
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import llm_models as models_svc
router = APIRouter(tags=["models"])


@router.get("/model/list", response_model=list[ModelOut])
async def list_models(
    response: Response,
    status: str | None = Query(None, description="active | disabled"),
    limit: int = Depends(limit_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await models_svc.list_models(
        db,
        owner_user_id=user_id,
        status=status,
        limit=limit,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.get("/model/{model_id}", response_model=ModelOut)
async def get_model(
    model_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await models_svc.get_model(db, model_id, owner_user_id=user_id)
    return require_entity(row, "模型不存在")


@router.post("/model", response_model=ModelOut)
async def create_model(
    body: ModelCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await models_svc.create_model(
        db,
        owner_user_id=user_id,
        name=body.name,
        provider=body.provider,
        model_name=body.model_name,
        api_key=body.api_key,
        base_url=body.base_url,
        temperature=body.temperature,
        top_p=body.top_p,
        max_tokens=body.max_tokens,
        timeout=body.timeout,
        config=body.config,
        status=body.status,
        model_id=body.id,
    )


@router.patch("/model/{model_id}", response_model=ModelOut)
async def update_model(
    model_id: str,
    body: ModelUpdate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await models_svc.update_model(
        db,
        model_id,
        owner_user_id=user_id,
        name=body.name,
        provider=body.provider,
        model_name=body.model_name,
        api_key=body.api_key,
        base_url=body.base_url,
        temperature=body.temperature,
        top_p=body.top_p,
        max_tokens=body.max_tokens,
        timeout=body.timeout,
        config=body.config,
        status=body.status,
    )


@router.delete("/model/{model_id}")
async def delete_model(
    model_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    await models_svc.delete_model(db, model_id, owner_user_id=user_id)
    return {"ok": True}


@router.post("/model/test", response_model=ModelTestResult)
async def test_model_inline(
    body: ModelTestRequest,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """
    连通性测试。

    - 传 ``model_id``：用目录已存配置测试
    - 否则用 body 内联字段（保存前试连）
    """
    # 仅当带了 model_id 且未提供新 api_key 时，才走目录已存配置；
    # 否则内联试连（避免编辑抽屉里新粘贴的 key 被 model_id 短路丢掉）。
    if body.model_id and not body.api_key:
        return await models_svc.test_model_by_id(
            db, body.model_id, owner_user_id=user_id
        )
    if not body.provider or not body.model_name:
        raise HTTPException(
            status_code=400,
            detail="未传 model_id 时必须提供 provider 与 model_name",
        )
    return await models_svc.test_model_connectivity(
        provider=body.provider,
        model_name=body.model_name,
        api_key=body.api_key,
        base_url=body.base_url,
        temperature=body.temperature,
        top_p=body.top_p,
        max_tokens=body.max_tokens,
        timeout=body.timeout,
        config=body.config,
    )


@router.post("/model/{model_id}/test", response_model=ModelTestResult)
async def test_model(
    model_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await models_svc.test_model_by_id(db, model_id, owner_user_id=user_id)
