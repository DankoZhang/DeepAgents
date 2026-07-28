"""
Middleware API（只读）
=====================

内置中间件由种子写入；前端仅可列表勾选，不可新建/编辑/删除。
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI：路由、依赖、HTTP 异常（无 Query：本模块无列表过滤参数）
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

# 只读响应模型（无 Create/Update schema）
from deepagents_app.api.schemas import MiddlewareOut
from deepagents_app.db.session import get_db
# 业务层：list / get（写接口已下线）
from deepagents_app.services import middlewares as mw_svc

# OpenAPI 分组
router = APIRouter(tags=["middlewares"])


@router.get("/middleware/list", response_model=list[MiddlewareOut])  # 全量列表供 Agent 勾选
def list_middlewares(db: Session = Depends(get_db)):
    # 返回种子写入的内置中间件元信息
    return mw_svc.list_middlewares(db)


@router.get("/middleware/{middleware_id}", response_model=MiddlewareOut)  # 单个详情
def get_middleware(middleware_id: str, db: Session = Depends(get_db)):
    # 按主键查
    row = mw_svc.get_middleware(db, middleware_id)
    if row is None:
        raise HTTPException(status_code=404, detail="中间件不存在")
    # ORM → MiddlewareOut
    return row
