"""
列表分页（服务层）
================

Keyset 游标翻页：无 ``cursor`` 为首页，有 ``cursor`` 从该点续翻。
API 层的 Query 依赖与响应头见 ``deepagents_app.api.pagination``。
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import datetime
from typing import Any, TypeVar

from sqlalchemy import Select, and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from deepagents_app.api.errors import BusinessError

T = TypeVar("T")

# 默认一页；客户端可显式加大，上限 MAX_LIMIT
DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def encode_cursor(*, sort: Any, id: str) -> str:
    """把排序键 + 主键编成 URL 安全游标。"""
    payload = {"s": sort, "i": id}
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def decode_cursor(cursor: str) -> tuple[Any, str]:
    """解析游标；非法则抛 ValueError。"""
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii"))
        payload = json.loads(raw.decode("utf-8"))
        sort = payload["s"]
        item_id = str(payload["i"])
    except Exception as exc:  # noqa: BLE001
        raise ValueError("非法分页游标") from exc
    if not item_id:
        raise ValueError("非法分页游标")
    return sort, item_id


async def paginate_keyset(
    db: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    limit: int,
    sort_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[str],
    cursor_sort: Any,
    cursor_id: str,
    descending: bool = False,
) -> tuple[list[T], int, bool]:
    """
    Keyset 分页：返回 (rows, total, has_more)。

    ``stmt`` 须已带与 keyset 一致的 ``order_by(sort_column, id_column)``。
    """
    count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
    total = int(await db.scalar(count_stmt) or 0)

    if descending:
        page_stmt = stmt.where(
            or_(
                sort_column < cursor_sort,
                and_(sort_column == cursor_sort, id_column < cursor_id),
            )
        )
    else:
        page_stmt = stmt.where(
            or_(
                sort_column > cursor_sort,
                and_(sort_column == cursor_sort, id_column > cursor_id),
            )
        )

    rows = list(await db.scalars(page_stmt.limit(limit + 1)))
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    return rows, total, has_more


def cursor_sort_value(row: Any, attr: str) -> Any:
    """取出用于编码游标的排序字段；datetime 转 iso。"""
    value = getattr(row, attr)
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def coerce_datetime(value: Any) -> datetime:
    """把游标里的排序值还原为 datetime。"""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return datetime.fromisoformat(value)
    raise ValueError("非法时间游标")


async def page_rows(
    db: AsyncSession,
    stmt: Select[tuple[T]],
    *,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    sort_column: InstrumentedAttribute[Any],
    id_column: InstrumentedAttribute[str],
    sort_attr: str,
    descending: bool = False,
    coerce_sort: Callable[[Any], Any] | None = None,
) -> tuple[list[T], int, str | None]:
    """
    Keyset 分页：无 ``cursor`` 取首页；有则从游标后续翻。

    返回 ``(rows, total, next_cursor)``；无更多时 ``next_cursor`` 为 None。
    """
    coerce = coerce_sort or (lambda v: v)

    if cursor:
        try:
            raw_sort, cursor_id = decode_cursor(cursor)
            cursor_sort = coerce(raw_sort)
        except (ValueError, TypeError, KeyError) as exc:
            raise BusinessError("非法分页游标") from exc
        rows, total, has_more = await paginate_keyset(
            db,
            stmt,
            limit=limit,
            sort_column=sort_column,
            id_column=id_column,
            cursor_sort=cursor_sort,
            cursor_id=cursor_id,
            descending=descending,
        )
    else:
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        total = int(await db.scalar(count_stmt) or 0)
        rows = list(await db.scalars(stmt.limit(limit + 1)))
        has_more = len(rows) > limit
        if has_more:
            rows = rows[:limit]

    next_cursor: str | None = None
    if has_more and rows:
        last = rows[-1]
        next_cursor = encode_cursor(
            sort=cursor_sort_value(last, sort_attr),
            id=getattr(last, "id"),
        )
    return rows, total, next_cursor
