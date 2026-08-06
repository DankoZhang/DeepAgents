"""
Middleware Registry
===================

数据库存元信息；运行时按 ``class_path`` 实例化。
"""

from __future__ import annotations

from typing import Any

from deepagents_app.db.models import MiddlewareDefinition
from deepagents_app.registries.tools import resolve_class_path


def load_middleware_object(mw_def: MiddlewareDefinition) -> Any:
    """加载并实例化 Middleware（支持无参类或已实例化对象）。"""
    obj = resolve_class_path(mw_def.class_path)
    cfg = dict(mw_def.config or {})
    if isinstance(obj, type):
        return obj(**cfg) if cfg else obj()
    if callable(obj) and not hasattr(obj, "name"):
        return obj(**cfg) if cfg else obj()
    return obj


def middleware_definition_from_snapshot(payload: dict[str, Any]) -> MiddlewareDefinition:
    """从快照 dict 构造脱离 Session 的 MiddlewareDefinition（仅供运行时实例化）。"""
    return MiddlewareDefinition(
        id=str(payload.get("id") or payload.get("name") or ""),
        name=str(payload.get("name") or ""),
        class_path=str(payload.get("class_path") or ""),
        config=dict(payload.get("config") or {}),
    )


def load_middlewares_from_snapshots(payloads: list[dict[str, Any]]) -> list[Any]:
    """按快照内嵌的中间件 payload 实例化（顺序保留）。"""
    return [
        load_middleware_object(middleware_definition_from_snapshot(p)) for p in payloads
    ]
