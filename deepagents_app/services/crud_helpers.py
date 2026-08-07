"""
Service 层 CRUD 样板
====================

收敛「按 owner 取行」「同名唯一」两类重复逻辑。
"""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError

T = TypeVar("T")


def get_owned(
    db: Session,
    model: type[T],
    row_id: str,
    *,
    owner_user_id: str,
) -> T | None:
    """``db.get`` + owner 校验；不存在或不属于当前用户返回 None。"""
    row = db.get(model, row_id)
    if row is None or getattr(row, "owner_user_id", None) != owner_user_id:
        return None
    return row


def ensure_unique_owned_name(
    db: Session,
    model: type[Any],
    *,
    owner_user_id: str,
    name: str,
    exclude_id: str | None = None,
    label: str,
    message: str | None = None,
) -> None:
    """
    断言 ``(owner_user_id, name)`` 唯一。

    ``exclude_id`` 用于更新改名时排除自身。
    ``message`` 可覆盖默认 ``已存在同名{label}：{name}`` 文案。
    """
    q = db.query(model).filter(
        model.owner_user_id == owner_user_id,  # type: ignore[attr-defined]
        model.name == name,  # type: ignore[attr-defined]
    )
    if exclude_id is not None:
        q = q.filter(model.id != exclude_id)  # type: ignore[attr-defined]
    if q.one_or_none() is not None:
        raise BusinessError(message or f"已存在同名{label}：{name}")
