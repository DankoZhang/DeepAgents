#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   errors.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   errors.py

业务异常与 FastAPI HTTP 映射。
"""

from __future__ import annotations

from typing import TypeVar

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

T = TypeVar("T")


class AppError(Exception):
    """可映射为 HTTP 响应的业务异常基类。"""


class NotFoundError(AppError):
    """资源不存在 → 404。"""


class ForbiddenError(AppError):
    """资源存在但不属于当前用户 → 403。"""


class BusinessError(AppError):
    """客户端可修复的业务校验失败 → 400。"""


class CapacityError(AppError):
    """进程内容量不足（如 SSE 并发满）→ 429。"""


def require_entity(row: T | None, detail: str) -> T:
    """``None`` → ``NotFoundError``；供路由层替代重复的 404 样板。"""
    if row is None:
        raise NotFoundError(detail)
    return row


def register_exception_handlers(app: FastAPI) -> None:
    """仅映射自有业务异常；内部 ValueError 等仍走 500。"""

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(ForbiddenError)
    async def _forbidden(_request: Request, exc: ForbiddenError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @app.exception_handler(BusinessError)
    async def _business(_request: Request, exc: BusinessError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(CapacityError)
    async def _capacity(_request: Request, exc: CapacityError) -> JSONResponse:
        return JSONResponse(
            status_code=429,
            content={"detail": str(exc)},
            headers={"Retry-After": "5"},
        )
