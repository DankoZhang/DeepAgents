"""
FastAPI 应用工厂
================

职责：
- 应用启动时初始化日志、全局 workspace memory（兼容旧路径）
- 挂载 CORS 与各业务路由

HarnessProfile / general-purpose 子 Agent 在组装时按方法论显式注入，
不再在 lifespan 全局注册。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from deepagents_app.api.errors import register_exception_handlers
from deepagents_app.api.routes import (
    agents,
    bootstrap,
    chat,
    conversations,
    llm_models,
    methodologies,
    middlewares,
    skills,
    tools,
)
from deepagents_app.config import get_settings
from deepagents_app.db.session import get_session_factory
from deepagents_app.factory import sync_memory_into_workspace

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    应用生命周期钩子。

    启动：日志、引擎、兼容旧路径的 AGENTS.md 同步。
    用户种子由 ``POST /api/bootstrap``（及 CLI）按用户幂等灌入，不在此预灌。
    """
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    get_session_factory()
    sync_memory_into_workspace(settings)

    logger.info(
        "DeepAgents API 已就绪（db=%s, auth_disabled=%s, cors=%s）",
        settings.database_url.split("@")[-1],
        settings.auth_disabled,
        settings.cors_origin_list(),
    )
    yield


def _check_db() -> str:
    try:
        db = get_session_factory()()
        try:
            db.execute(text("SELECT 1"))
            return "ok"
        finally:
            db.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health db check failed: %s", exc)
        return "error"


def _check_redis(redis_url: str) -> str:
    try:
        import redis

        client = redis.Redis.from_url(redis_url, socket_connect_timeout=1.5)
        try:
            if client.ping():
                return "ok"
            return "error"
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("health redis check failed: %s", exc)
        return "error"


def create_app() -> FastAPI:
    """创建并配置 FastAPI 实例（可被 uvicorn / 测试复用）。"""
    settings = get_settings()
    app = FastAPI(
        title="DeepAgents Methodology Platform",
        description="可配置方法论驱动的多 Agent 平台（MVP）",
        version="0.2.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    origins = list(settings.cors_origin_list())
    if not origins:
        origins = ["http://localhost:5173"]
    if "*" in origins:
        logger.warning(
            "CORS_ORIGINS 含 * 且 enable credentials 时不合规，已忽略 *；请改为具体前端源"
        )
        origins = [o for o in origins if o != "*"] or ["http://localhost:5173"]

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(bootstrap.router, prefix="/api")
    app.include_router(llm_models.router, prefix="/api")
    app.include_router(methodologies.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(skills.router, prefix="/api")
    app.include_router(tools.router, prefix="/api")
    app.include_router(middlewares.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")

    @app.get("/health")
    def health(response: Response) -> dict[str, Any]:
        """
        探活：检查进程 + DB + Redis。

        - db 失败 → 503 / status=error
        - redis 失败且 REQUIRE_REDIS_CHECKPOINTER=true → 503
        - redis 失败但未强制 → 200 / status=degraded
        """
        cfg = get_settings()
        db_status = _check_db()
        redis_status = _check_redis(cfg.redis_url)

        overall = "ok"
        if db_status != "ok":
            overall = "error"
        elif redis_status != "ok" and cfg.require_redis_checkpointer:
            overall = "error"
        elif redis_status != "ok":
            overall = "degraded"

        if overall == "error":
            response.status_code = 503

        return {
            "status": overall,
            "checks": {
                "db": db_status,
                "redis": redis_status,
            },
        }

    return app


app = create_app()
