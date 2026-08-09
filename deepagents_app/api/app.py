"""
FastAPI 应用工厂
================

职责：
- 应用启动时初始化日志、引擎、可选 Skills / content_blob GC 后台
- 挂载 CORS 与各业务路由

HarnessProfile / general-purpose 子 Agent 在组装时按方法论显式注入，
不再在 lifespan 全局注册。
"""

from __future__ import annotations

import logging
import threading
import time
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
from deepagents_app.auth import close_auth_http_client
from deepagents_app.config import get_settings
from deepagents_app.db.session import get_async_engine, get_async_session_factory
from deepagents_app.factory import close_checkpointer, init_checkpointer
from deepagents_app.services.cache_pubsub import (
    start_cache_invalidation_listener,
    stop_cache_invalidation_listener,
)
from deepagents_app.services.chat import close_redis_stream_slots_client
from deepagents_app.services.content_blobs_gc_scheduler import (
    start_content_blob_gc_scheduler,
    stop_content_blob_gc_scheduler,
)
from deepagents_app.services.skills_gc_scheduler import (
    start_skills_gc_scheduler,
    stop_skills_gc_scheduler,
)

logger = logging.getLogger(__name__)

_health_lock = threading.Lock()
# (monotonic_ts, body, http_status) — 仅缓存成功探活
_health_cache: tuple[float, dict[str, Any], int] | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """
    应用生命周期钩子。

    启动：日志、引擎、AsyncRedisSaver、鉴权配置校验、GC 后台（可选）。
    用户种子由 ``POST /api/bootstrap`` 按用户幂等灌入，不在此预灌。
    Memory 随方法论快照版本化；组装时按 version 物化。
    """
    settings = get_settings()
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    if settings.auth_disabled:
        logger.warning(
            "AUTH_DISABLED=true：跳过外部鉴权（仅限本地/测试）；生产必须设为 false"
        )
    elif not (settings.auth_introspect_url or "").strip():
        raise RuntimeError(
            "AUTH_DISABLED=false 时必须配置 AUTH_INTROSPECT_URL"
        )

    get_async_session_factory()
    await init_checkpointer(settings)
    await start_cache_invalidation_listener()
    start_skills_gc_scheduler(settings)
    start_content_blob_gc_scheduler(settings)

    logger.info(
        "DeepAgents API 已就绪（db=%s, auth_disabled=%s, cors=%s, workers=%s）",
        settings.database_url.split("@")[-1],
        settings.auth_disabled,
        settings.cors_origin_list(),
        settings.api_workers,
    )
    try:
        yield
    finally:
        await stop_content_blob_gc_scheduler()
        await stop_skills_gc_scheduler()
        await stop_cache_invalidation_listener()
        await close_redis_stream_slots_client()
        await close_checkpointer()
        await close_auth_http_client()


async def _check_db() -> str:
    try:
        engine = get_async_engine()
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("health db check failed: %s", exc)
        return "error"


async def _check_redis(redis_url: str) -> str:
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(redis_url, socket_connect_timeout=1.5)
        try:
            if await client.ping():
                return "ok"
            return "error"
        finally:
            await client.aclose()
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
        # 前端 cursor 翻页依赖这两个响应头（跨域时必须 expose）
        expose_headers=["X-Total-Count", "X-Next-Cursor"],
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
    async def health(response: Response) -> dict[str, Any]:
        """
        探活：检查进程 + DB + Redis（成功结果短 TTL 缓存）。

        - db / redis 失败 → 503 / status=error（失败不缓存，便于快速恢复）
        """
        global _health_cache

        cfg = get_settings()
        ttl = float(cfg.health_cache_ttl_seconds or 0)
        now = time.monotonic()
        if ttl > 0:
            with _health_lock:
                if _health_cache is not None:
                    cached_at, body, code = _health_cache
                    if now - cached_at < ttl:
                        response.status_code = code
                        return body

        db_status = await _check_db()
        redis_status = await _check_redis(cfg.redis_url)

        overall = "ok"
        if db_status != "ok" or redis_status != "ok":
            overall = "error"

        code = 503 if overall == "error" else 200
        body = {
            "status": overall,
            "checks": {
                "db": db_status,
                "redis": redis_status,
            },
        }
        response.status_code = code

        # 只缓存成功探活，失败实时重试
        if ttl > 0 and overall == "ok":
            with _health_lock:
                _health_cache = (now, body, code)

        return body

    return app


app = create_app()
