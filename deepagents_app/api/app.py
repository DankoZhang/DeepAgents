"""FastAPI 应用工厂。"""

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
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    init_db()
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
    logger.info("DeepAgents API 已就绪（db=%s）", settings.database_url.split("@")[-1])
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="DeepAgents Methodology Platform",
        description="可配置方法论驱动的多 Agent 平台（MVP）",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(methodologies.router, prefix="/api")
    app.include_router(agents.router, prefix="/api")
    app.include_router(tools.router, prefix="/api")
    app.include_router(middlewares.router, prefix="/api")
    app.include_router(conversations.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
