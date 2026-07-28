"""
Middleware 注册管理
==================

对外 API 只读；create/update/delete 供种子与内部使用。
"""

# 推迟注解求值
from __future__ import annotations

# 生成中间件主键
import uuid
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import MiddlewareDefinition
# 变更后清 Agent 缓存（内部写接口仍可能调用）
from deepagents_app.services.agent_factory import invalidate_agent_cache


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


def update_middleware(
    db: Session,
    middleware_id: str,
    *,
    name: str | None = None,
    class_path: str | None = None,
    config: dict[str, Any] | None = None,
) -> MiddlewareDefinition:
    # 内部用；对外写 API 已下线
    row = db.get(MiddlewareDefinition, middleware_id)
    if row is None:
        raise LookupError(f"中间件不存在：{middleware_id}")
    if name is not None:
        row.name = name
    if class_path is not None:
        row.class_path = class_path
    if config is not None:
        row.config = config
    invalidate_agent_cache()  # 已编译图可能挂着旧实例
    db.flush()
    return row


def delete_middleware(db: Session, middleware_id: str) -> None:
    # 内部用；对外不可删内置中间件的产品策略由路由层体现
    row = db.get(MiddlewareDefinition, middleware_id)
    if row is None:
        raise LookupError(f"中间件不存在：{middleware_id}")
    invalidate_agent_cache()
    db.delete(row)
    db.flush()
