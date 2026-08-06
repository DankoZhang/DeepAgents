"""
列表分页约定
============

统一 ``limit`` / ``offset``；响应仍返回数组以兼容现有前端，
总条数通过 ``X-Total-Count`` 响应头暴露。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import Query, Response
from sqlalchemy.orm import Query as SAQuery

# 默认拉满一页上限，兼容尚未接分页 UI 的前端；真分页时显式传更小 limit
DEFAULT_LIMIT = 200
MAX_LIMIT = 200

T = TypeVar("T")


def limit_query(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="每页条数"),
) -> int:
    return limit


def offset_query(
    offset: int = Query(0, ge=0, description="跳过条数"),
) -> int:
    return offset


def paginate_query(q: SAQuery, *, limit: int, offset: int) -> tuple[list, int]:
    """对 SQLAlchemy Query 做 count + slice，返回 (rows, total)。"""
    total = q.count()
    rows = q.offset(offset).limit(limit).all()
    return rows, total


def set_total_count(response: Response, total: int) -> None:
    """写入列表总条数，供前端分页控件使用。"""
    response.headers["X-Total-Count"] = str(total)
