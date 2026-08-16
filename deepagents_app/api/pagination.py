#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   pagination.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   pagination.py

列表分页约定
============

统一 ``limit`` / 可选 ``cursor``；响应仍返回数组，
总条数通过 ``X-Total-Count``，下一页游标通过 ``X-Next-Cursor``。

服务层请从 ``deepagents_app.db.pagination`` 导入 ``page_rows``
与 ``DEFAULT_LIMIT``；本模块只保留 FastAPI Query 依赖与响应头辅助。
"""

from __future__ import annotations

from fastapi import Query, Response

from deepagents_app.db.pagination import DEFAULT_LIMIT, MAX_LIMIT

__all__ = [
    "limit_query",
    "cursor_query",
    "set_total_count",
    "set_next_cursor",
]


def limit_query(
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="每页条数"),
) -> int:
    return limit


def cursor_query(
    cursor: str | None = Query(
        None,
        description="keyset 游标（上一页最后一条的 X-Next-Cursor）",
    ),
) -> str | None:
    return cursor


def set_total_count(response: Response, total: int) -> None:
    """写入列表总条数，供前端分页控件使用。"""
    response.headers["X-Total-Count"] = str(total)


def set_next_cursor(response: Response, cursor: str | None) -> None:
    """写入下一页游标；无更多结果时删除该头。"""
    if cursor:
        response.headers["X-Next-Cursor"] = cursor
    elif "X-Next-Cursor" in response.headers:
        del response.headers["X-Next-Cursor"]
