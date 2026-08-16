#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   crud_helpers.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   crud_helpers.py

Service 层 CRUD 样板
====================

收敛「按 owner 取行」「同名唯一」「创建时解析主键」三类重复逻辑。
"""

from __future__ import annotations

import uuid
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError
from deepagents_app.ownership import validate_resource_id

T = TypeVar("T")


def resolve_resource_id(
    resource_id: str | None,
    *,
    prefix: str,
    label: str,
) -> str:
    """创建资源时：客户端指定 id 则校验，否则 ``{prefix}{12hex}`` 自动生成。"""
    resolved = resource_id or f"{prefix}{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label=label)


async def get_owned(
    db: AsyncSession,
    model: type[T],
    row_id: str,
    *,
    owner_user_id: str,
) -> T | None:
    """``db.get`` + owner 校验；不存在或不属于当前用户返回 None。"""
    row = await db.get(model, row_id)
    if row is None or getattr(row, "owner_user_id", None) != owner_user_id:
        return None
    return row


async def ensure_unique_owned_name(
    db: AsyncSession,
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
    stmt = select(model).where(
        model.owner_user_id == owner_user_id,  # type: ignore[attr-defined]
        model.name == name,  # type: ignore[attr-defined]
    )
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)  # type: ignore[attr-defined]
    if (await db.scalars(stmt)).one_or_none() is not None:
        raise BusinessError(message or f"已存在同名{label}：{name}")
