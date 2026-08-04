"""
FastAPI 应用工厂
================

职责：
- 应用启动时初始化日志、写入幂等种子数据（schema 须已由 migrate 准备好）
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
    llm_models,
    methodologies,
    middlewares,
    skills,
    tools,
)
from deepagents_app.config import get_settings
from deepagents_app.db.seed import seed_defaults
from deepagents_app.db.session import get_session_factory

logger = logging.getLogger(__name__)


@asynccontextmanager  # 把下方异步生成器包装成「可 async with」的上下文管理器
async def lifespan(_app: FastAPI):
    """
    应用生命周期钩子（由 FastAPI 在启动/关闭时自动调用）。

    启动阶段（yield 之前）：
    1. 按配置初始化根日志
    2. 幂等写入内置 Tool / Middleware / demo 方法论
       （表结构请先执行 ``python -m deepagents_app.db.migrate``）
    关闭阶段（yield 之后）：当前无额外清理逻辑
    """
    # 读取进程配置（.env / 环境变量），含 database_url、log_level 等
    settings = get_settings()
    # 配置根 logger：之后 logger.info 等才会按该级别与格式输出
    logging.basicConfig(
        # 把配置字符串（如 "INFO"）转成 logging 常量；非法值则回退 INFO
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        # 日志行格式：时间 | 级别 | logger 名 | 消息
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        # 时间只显示到秒，省略毫秒与日期
        datefmt="%H:%M:%S",
    )

    # 取 sessionmaker（副作用：若引擎尚未创建则先 get_engine 懒初始化）
    # 详见 deepagents_app.db.session.get_session_factory / get_engine
    factory = get_session_factory()
    # 打开一条新的 DB Session（独立于请求里 Depends(get_db) 的会话）
    db = factory()
    try:
        # 幂等写入内置 Tool / Middleware / demo 方法论等种子数据
        seed_defaults(db)
        # 种子写入成功则提交事务，持久化到数据库
        db.commit()
    except Exception:
        # 任一步失败：回滚未提交变更，避免半写入状态
        db.rollback()
        # 打出完整堆栈，便于排查启动失败原因
        logger.exception(
            "种子数据写入失败（若提示表/列不存在，请先执行: "
            "python -m deepagents_app.db.migrate）"
        )
        # 重新抛出：阻止 FastAPI 进入「已就绪」状态
        raise
    finally:
        # 无论成功失败都关闭 Session，归还连接池中的连接
        db.close()

    # 只打印 URL 中 @ 之后的 host/db 段，避免把账号密码打到 stdout
    logger.info("DeepAgents API 已就绪（db=%s）", settings.database_url.split("@")[-1])
    # yield 之前 = 启动完成；yield 之后 = 进程关闭时继续执行下方代码
    yield
    # 关闭阶段：引擎为进程内单例，暂无需要显式 dispose / 关闭的资源


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


# uvicorn deepagents_app.api.app:app 直接引用此实例
app = create_app()
