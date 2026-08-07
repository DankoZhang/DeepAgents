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

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    启动：日志、引擎、兼容旧路径的 AGENTS.md 同步；
    AUTH_DISABLED 时为开发用户预引导种子。
    """
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    get_session_factory()
    sync_memory_into_workspace(settings)

    if settings.auth_disabled and settings.auth_dev_user_id:
        from deepagents_app.db.bootstrap_session import bootstrapped_db_session

        with bootstrapped_db_session(settings.auth_dev_user_id) as _db:
            logger.info("已为开发用户引导种子：%s", settings.auth_dev_user_id)
    logger.info(
        "DeepAgents API 已就绪（db=%s, auth_disabled=%s）",
        settings.database_url.split("@")[-1],
        settings.auth_disabled,
    )
    yield


def create_app() -> FastAPI:
    """创建并配置 FastAPI 实例（可被 uvicorn / 测试复用）。"""
    app = FastAPI(
        title="DeepAgents Methodology Platform",
        description="可配置方法论驱动的多 Agent 平台（MVP）",
        version="0.2.0",
        lifespan=lifespan,
    )

    register_exception_handlers(app)

    # MVP 放开全部来源；生产应改为前端域名白名单
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
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
    def health() -> dict[str, str]:
        """进程探活，不检查 DB / Redis。"""
        return {"status": "ok"}

    return app


app = create_app()
