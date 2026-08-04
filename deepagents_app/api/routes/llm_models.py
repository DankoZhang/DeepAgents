"""
大模型目录 API
==============

前端统一配置 provider / 模型名 / 超参数，并支持连通性测试。
Agent 通过 model_id 绑定目录中的模型（方案 B）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import (
    ModelCreate,
    ModelOut,
    ModelTestRequest,
    ModelTestResult,
    ModelUpdate,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import llm_models as models_svc

router = APIRouter(tags=["models"])


def _to_out(row) -> ModelOut:  # noqa: ANN001
    return ModelOut(
        id=row.id,
        name=row.name,
        provider=row.provider,
        model_name=row.model_name,
        base_url=row.base_url,
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=row.max_tokens,
        context_length=row.context_length,
        timeout=row.timeout,
        config=dict(row.config or {}),
        status=row.status,
        has_api_key=bool(row.api_key),
        created_time=row.created_time,
        updated_time=row.updated_time,
    )


@router.get("/model/list", response_model=list[ModelOut])
def list_models(
    status: str | None = Query(None, description="active | disabled"),
    db: Session = Depends(get_db),
):
    return [_to_out(r) for r in models_svc.list_models(db, status=status)]


@router.get("/model/{model_id}", response_model=ModelOut)
def get_model(model_id: str, db: Session = Depends(get_db)):
    row = models_svc.get_model(db, model_id)
    if row is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return _to_out(row)


@router.post("/model", response_model=ModelOut)
def create_model(body: ModelCreate, db: Session = Depends(get_db)):
    try:
        row = models_svc.create_model(
            db,
            name=body.name,
            provider=body.provider,
            model_name=body.model_name,
            api_key=body.api_key,
            base_url=body.base_url,
            temperature=body.temperature,
            top_p=body.top_p,
            max_tokens=body.max_tokens,
            context_length=body.context_length,
            timeout=body.timeout,
            config=body.config,
            status=body.status,
            model_id=body.id,
        )
        return _to_out(row)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/model/{model_id}", response_model=ModelOut)
def update_model(model_id: str, body: ModelUpdate, db: Session = Depends(get_db)):
    try:
        row = models_svc.update_model(
            db,
            model_id,
            name=body.name,
            provider=body.provider,
            model_name=body.model_name,
            api_key=body.api_key,
            clear_api_key=body.clear_api_key,
            base_url=body.base_url,
            temperature=body.temperature,
            top_p=body.top_p,
            max_tokens=body.max_tokens,
            context_length=body.context_length,
            timeout=body.timeout,
            config=body.config,
            status=body.status,
        )
        return _to_out(row)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/model/{model_id}")
def delete_model(model_id: str, db: Session = Depends(get_db)):
    try:
        models_svc.delete_model(db, model_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True}


@router.post("/model/test", response_model=ModelTestResult)
def test_model_inline(body: ModelTestRequest, db: Session = Depends(get_db)):
    """
    连通性测试。

    - 传 ``model_id``：用目录已存配置测试
    - 否则用 body 内联字段（保存前试连）
    """
    try:
        if body.model_id:
            result = models_svc.test_model_by_id(db, body.model_id)
        else:
            if not body.provider or not body.model_name:
                raise HTTPException(
                    status_code=400,
                    detail="未传 model_id 时必须提供 provider 与 model_name",
                )
            result = models_svc.test_model_connectivity(
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
        return result
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/model/{model_id}/test", response_model=ModelTestResult)
def test_model(model_id: str, db: Session = Depends(get_db)):
    try:
        return models_svc.test_model_by_id(db, model_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
