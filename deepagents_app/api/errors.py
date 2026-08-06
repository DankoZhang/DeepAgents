"""业务异常与 FastAPI HTTP 映射。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """可映射为 HTTP 响应的业务异常基类。"""


class NotFoundError(AppError):
    """资源不存在 → 404。"""


class BusinessError(AppError):
    """客户端可修复的业务校验失败 → 400。"""


def register_exception_handlers(app: FastAPI) -> None:
    """仅映射自有业务异常；内部 ValueError 等仍走 500。"""

    @app.exception_handler(NotFoundError)
    async def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(BusinessError)
    async def _business(_request: Request, exc: BusinessError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
