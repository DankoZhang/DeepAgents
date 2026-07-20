"""
FastAPI 应用工厂
================

职责：
- 应用启动时初始化日志、建表、写入幂等种子数据
- 挂载 CORS 与各业务路由（方法论 / Agent / Tool / 会话 / 聊天）
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deepagents_app.api.routes import (
    agents,
    chat,
    conversations,
    methodologies,
    middlewares,
    tools,
)
from deepagents_app.config import get_settings
from deepagents_app.db.seed import seed_defaults
from deepagents_app.db.session import get_session_factory, init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    应用生命周期钩子。

    启动阶段（yield 之前）：
    1. 按配置初始化根日志
    2. ``create_all`` 建表（MVP；生产可换 Alembic）
    3. 幂等写入内置 Tool / Middleware / demo 方法论
    """
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    # 1) 建表：确保 ORM 模型已注册到 metadata
    init_db()

    # 2) 种子数据：独立 Session，失败则回滚并阻止服务就绪
    factory = get_session_factory()
    db = factory()
    try:
        seed_defaults(db)
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("种子数据写入失败")
        raise
    finally:
        db.close()

    # 日志只打印 host/db 段，避免把账号密码打到 stdout
    logger.info("DeepAgents API 已就绪（db=%s）", settings.database_url.split("@")[-1])
    yield
    # 关闭阶段暂无资源需要显式释放（引擎为进程内单例）


def create_app() -> FastAPI:
    """创建并配置 FastAPI 实例（可被 uvicorn / 测试复用）。"""
    app = FastAPI(
        title="DeepAgents Methodology Platform",
        description="可配置方法论驱动的多 Agent 平台（MVP）",
        version="0.2.0",
        lifespan=lifespan,
    )

    # MVP 放开全部来源；生产应改为前端域名白名单
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 业务 API 统一挂在 /api 下；/health 单独暴露给探活
    app.include_router(methodologies.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(tools.router, prefix="/api")
    app.include_router(middlewares.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")

    @app.get("/health")
    def health() -> dict[str, str]:
        """进程探活，不检查 DB / Redis。"""
        return {"status": "ok"}

    return app


# uvicorn deepagents_app.api.app:app 直接引用此实例
app = create_app()
