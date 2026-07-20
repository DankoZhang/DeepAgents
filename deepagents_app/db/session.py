"""
数据库会话与引擎
================

同步 SQLAlchemy 2.0（MVP 足够；后续可换 async）。
"""

from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from deepagents_app.config import get_settings
from deepagents_app.db.base import Base

_engine = None
_SessionLocal: sessionmaker[Session] | None = None


def reset_engine() -> None:
    """测试用：关闭并清空进程内引擎缓存。"""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def get_engine():
    """懒初始化引擎（进程内单例）。"""
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        connect_args = {}
        # SQLite 默认禁止跨线程共用连接；FastAPI 多线程下需关闭该检查
        if settings.database_url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
        _engine = create_engine(
            settings.database_url,
            pool_pre_ping=True,  # 取连接前探测，避免拿到已断开的连接
            future=True,
            connect_args=connect_args,
        )
        if settings.database_url.startswith("sqlite"):
            # SQLite 默认不强制 FK；与 PostgreSQL 行为对齐
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

        _SessionLocal = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            # 提交后仍可访问属性，避免路由层序列化时触发 DetachedInstanceError
            expire_on_commit=False,
        )
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回已绑定引擎的 sessionmaker（副作用：触发引擎初始化）。"""
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


def init_db() -> None:
    """创建全部表（MVP 用 create_all；后续可换 Alembic）。"""
    # 确保模型已注册到 Base.metadata（仅 import 副作用）
    import deepagents_app.db.models  # noqa: F401

    engine = get_engine()
    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """
    FastAPI Depends 用的会话生成器。

    请求成功结束自动 commit；异常则 rollback。
    路由内一般不必手动 commit。
    """
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
