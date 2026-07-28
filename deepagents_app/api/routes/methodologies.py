"""
方法论 API
==========

CRUD + 发布 + 勾选全局 Agent + 版本列表。
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI 路由基础设施
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

# 方法论相关请求/响应模型
from deepagents_app.api.schemas import (
    MethodologyBindAgents,  # POST .../agents body
    MethodologyCreate,  # POST /methodology body
    MethodologyDetailOut,  # 含 agents 的详情响应
    MethodologyOut,  # 列表/简要响应
    MethodologyUpdate,  # PATCH body
)
from deepagents_app.db.session import get_db
# 业务层方法论服务
from deepagents_app.services import methodology as methodology_svc

# OpenAPI 分组：methodology
router = APIRouter(tags=["methodology"])


@router.post("/methodology", response_model=MethodologyDetailOut)  # 创建草稿
def create_methodology(body: MethodologyCreate, db: Session = Depends(get_db)):
    """创建草稿方法论，可选 ``agent_ids`` 立即勾选全局 Agent。"""
    try:
        return methodology_svc.create_methodology(
            db,
            name=body.name,  # 显示名
            description=body.description,  # 说明
            methodology_id=body.id,  # 可选指定 id，否则 slug 生成
            agent_ids=body.agent_ids or None,  # 空列表当 None，跳过绑定
        )
    except ValueError as exc:
        # 如 id 已存在 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        # agent_ids 里有不存在的 Agent → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/methodology/list", response_model=list[MethodologyOut])  # 列表
def list_methodologies(
    # 可选按状态过滤
    status: str | None = Query(None, description="draft | published | archived"),
    db: Session = Depends(get_db),
):
    # 返回简要列表（不含 agents 明细）
    return methodology_svc.list_methodologies(db, status=status)


@router.get("/methodology/{methodology_id}", response_model=MethodologyDetailOut)  # 详情
def get_methodology(methodology_id: str, db: Session = Depends(get_db)):
    # 含已勾选 Agent 及其 tools/middlewares
    row = methodology_svc.get_methodology(db, methodology_id)
    if row is None:
        raise HTTPException(status_code=404, detail="方法论不存在")
    return row


@router.patch("/methodology/{methodology_id}", response_model=MethodologyOut)  # 改元信息
def update_methodology(
    methodology_id: str,  # 路径 id
    body: MethodologyUpdate,  # name/description/bump_version
    db: Session = Depends(get_db),
):
    try:
        return methodology_svc.update_methodology(
            db,
            methodology_id,
            name=body.name,  # None 不改
            description=body.description,
            bump_version=body.bump_version,  # True 则 version+1 并快照
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/methodology/{methodology_id}")  # 删除方法论
def delete_methodology(methodology_id: str, db: Session = Depends(get_db)):
    try:
        # 删方法论行；勾选关系 cascade；不删全局 Agent
        methodology_svc.delete_methodology(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}


@router.post(
    "/methodology/{methodology_id}/agents",  # 勾选全局 Agent
    response_model=MethodologyDetailOut,  # 返回更新后的详情
)
def bind_agents(
    methodology_id: str,
    body: MethodologyBindAgents,  # agent_ids + replace
    db: Session = Depends(get_db),
):
    """勾选全局 Agent；``replace=True`` 时先清空再绑定。"""
    try:
        return methodology_svc.bind_methodology_agents(
            db,
            methodology_id,
            body.agent_ids,  # 要纳入的全局 Agent id 列表
            replace=body.replace,  # True：整表替换勾选
        )
    except LookupError as exc:
        # 方法论或某个 agent 不存在
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/methodology/{methodology_id}/publish", response_model=MethodologyOut)  # 发布
def publish_methodology(methodology_id: str, db: Session = Depends(get_db)):
    try:
        # 校验至少有一个 supervisor，status → published，写快照
        return methodology_svc.publish_methodology(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 缺少 Supervisor 等业务错误 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/methodology/{methodology_id}/versions")  # 历史版本列表
def list_versions(methodology_id: str, db: Session = Depends(get_db)):
    try:
        # 返回 [{methodology_id, version, created_time}, ...]
        return methodology_svc.get_methodology_versions(db, methodology_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
