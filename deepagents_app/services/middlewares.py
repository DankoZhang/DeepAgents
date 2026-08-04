"""
Middleware 注册管理
==================

对外 API 只读；``create_middleware`` 供种子与内部使用。
"""

# 推迟注解求值
from __future__ import annotations

# 生成中间件主键
import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import MiddlewareDefinition


def list_middlewares(db: Session) -> list[MiddlewareDefinition]:
    # 按名称排序，供前端勾选
    return db.query(MiddlewareDefinition).order_by(MiddlewareDefinition.name).all()


def get_middleware(db: Session, middleware_id: str) -> MiddlewareDefinition | None:
    # 主键查询；不存在返回 None
    return db.get(MiddlewareDefinition, middleware_id)


def create_middleware(
    db: Session,
    *,
    name: str,  # 全局唯一名
    class_path: str,  # module:Class 导入路径
    config: dict[str, Any] | None = None,  # 构造参数
    middleware_id: str | None = None,  # 种子可固定 id
) -> MiddlewareDefinition:
    # 名称唯一校验
    if (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.name == name)
        .one_or_none()
    ):
        raise ValueError(f"中间件名已存在：{name}")
    row = MiddlewareDefinition(
        id=middleware_id or f"mw_{uuid.uuid4().hex[:12]}",
        name=name,
        class_path=class_path,
        config=config or {},
    )
    db.add(row)
    db.flush()
    return row
