"""
数据库会话与引擎
================

同时提供：
- **同步** engine：供 Alembic ``migrate_db``
- **异步** engine / AsyncSession：供 FastAPI API 与业务服务层

Schema 变更由 Alembic 管理：部署/本地用 ``migrate_db``（或
``python -m deepagents_app.db.migrate``），**不在** API 启动时自动执行。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from deepagents_app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

# 进程内引擎单例缓存；None 表示尚未 create_engine
_engine = None
_async_engine: AsyncEngine | None = None
_AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None

_ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def _to_async_database_url(url: str) -> str:
    """同步 URL → 异步驱动 URL（sqlite 换 aiosqlite；postgres+psycopg 可原样给 async）。"""
    if url.startswith("sqlite+pysqlite://"):
        return "sqlite+aiosqlite://" + url.removeprefix("sqlite+pysqlite://")
    if url.startswith("sqlite://"):
        return "sqlite+aiosqlite://" + url.removeprefix("sqlite://")
    return url


def reset_engine() -> None:
    """测试用：关闭并清空进程内同步/异步引擎缓存（换 DATABASE_URL 后必须调用）。"""
    global _engine, _async_engine, _AsyncSessionLocal
    if _engine is not None:
        _engine.dispose()
    if _async_engine is not None:
        # aiosqlite 连接必须在事件循环内 dispose；独立线程跑 asyncio.run 最稳妥
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        engine = _async_engine

        def _dispose_sync() -> None:
            asyncio.run(engine.dispose())

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            _dispose_sync()
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(_dispose_sync).result()
    _engine = None
    _async_engine = None
    _AsyncSessionLocal = None


def get_engine():
    """懒初始化同步引擎（进程内单例）；仅供 Alembic migrate_db 使用。"""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        is_sqlite = settings.database_url.startswith("sqlite")
        if is_sqlite:
            connect_args["check_same_thread"] = False
        pool_kwargs: dict = {}
        if not is_sqlite:
            pool_kwargs = {
                "pool_size": int(settings.db_pool_size),
                "max_overflow": int(settings.db_max_overflow),
                "pool_timeout": float(settings.db_pool_timeout),
                "pool_recycle": int(settings.db_pool_recycle) or -1,
            }
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,
            future=True,
            connect_args=connect_args,
            **pool_kwargs,
        )
        if is_sqlite:

            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    return _engine


def get_async_engine() -> AsyncEngine:
    """懒初始化异步引擎（进程内单例）；API / 服务层使用。"""
    global _async_engine, _AsyncSessionLocal
    if _async_engine is None:
        settings = get_settings()
        url = _to_async_database_url(settings.database_url)
        connect_args = {}
        is_sqlite = url.startswith("sqlite")
        if is_sqlite:
            connect_args["check_same_thread"] = False
        pool_kwargs: dict = {}
        if not is_sqlite:
            pool_kwargs = {
                "pool_size": int(settings.db_pool_size),
                "max_overflow": int(settings.db_max_overflow),
                "pool_timeout": float(settings.db_pool_timeout),
                "pool_recycle": int(settings.db_pool_recycle) or -1,
            }
        _async_engine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
            **pool_kwargs,
        )
        if is_sqlite:

            @event.listens_for(_async_engine.sync_engine, "connect")
            def _set_sqlite_pragma_async(dbapi_connection, _connection_record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        _AsyncSessionLocal = async_sessionmaker(
            bind=_async_engine,
            class_=AsyncSession,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _async_engine


def get_async_session_factory() -> async_sessionmaker[AsyncSession]:
    """返回已绑定异步引擎的 async_sessionmaker（副作用：触发异步引擎初始化）。"""
    get_async_engine()
    assert _AsyncSessionLocal is not None
    return _AsyncSessionLocal


def _alembic_config(database_url: str | None = None) -> Config:
    """构造指向项目根 alembic.ini 的 Config，并写入当前 DATABASE_URL。"""
    if not _ALEMBIC_INI.is_file():
        raise FileNotFoundError(f"找不到 Alembic 配置：{_ALEMBIC_INI}")
    cfg = Config(str(_ALEMBIC_INI))
    url = database_url or get_settings().database_url
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def migrate_db(database_url: str | None = None) -> None:
    """执行 ``alembic upgrade head``（部署步骤 / CLI；API 启动不会调用）。

    - 空库：新建全部表
    - 已是最新：不变
    - 有未应用迁移：更新表
    """
    import deepagents_app.db.models  # noqa: F401

    get_engine()
    cfg = _alembic_config(database_url)
    logger.info("执行 alembic upgrade head")
    command.upgrade(cfg, "head")


async def get_async_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI Depends：请求成功 commit，异常 rollback；commit 后执行缓存失效。"""
    factory = get_async_session_factory()
    db = factory()
    try:
        yield db
        await db.commit()
    except Exception:
        await db.rollback()
        raise
    else:
        # 事务已提交，缓存广播失败不能把已成功的写操作伪装成 HTTP 500。
        try:
            from deepagents_app.services.versioning.revisions import flush_cache_invalidations

            flush_cache_invalidations(db)
        except Exception:  # noqa: BLE001
            logger.exception("提交后的 Agent 缓存失效失败")
    finally:
        await db.close()
