"""
数据库会话与引擎
================

同步 SQLAlchemy 2.0（MVP 足够；后续可换 async）。
Schema 变更由 Alembic 管理：部署/本地用 ``migrate_db``（或
``python -m deepagents_app.db.migrate``），**不在** API 启动时自动执行。
"""

from __future__ import annotations

import logging
from collections.abc import Generator

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from deepagents_app.config import PROJECT_ROOT, get_settings

logger = logging.getLogger(__name__)

# 进程内引擎单例缓存；None 表示尚未 create_engine
_engine = None
# 与引擎绑定的 sessionmaker 缓存；由 get_engine 一并初始化
_SessionLocal: sessionmaker[Session] | None = None

_ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"


def reset_engine() -> None:
    """测试用：关闭并清空进程内引擎缓存（换 DATABASE_URL 后必须调用）。"""
    # 声明要改的是模块级变量，而非函数内局部变量
    global _engine, _SessionLocal
    # 若引擎已创建，先释放连接池中的物理连接
    if _engine is not None:
        _engine.dispose()
    # 清空缓存，下次 get_engine() 会按新配置重新建引擎
    _engine = None
    _SessionLocal = None


def get_engine():
    """懒初始化引擎（进程内单例）；lifespan / migrate_db / get_db 最终都会走到这里。"""
    # 需要写入模块级单例
    global _engine, _SessionLocal
    # 已初始化则直接复用，避免重复建连接池
    if _engine is None:
        # 读取 DATABASE_URL 等配置（与 app.lifespan 共用同一份 settings）
        settings = get_settings()
        # 驱动级额外参数；Postgres 通常为空，SQLite 测试环境会填入
        connect_args = {}
        # SQLite 默认禁止跨线程共用同一连接；FastAPI / TestClient 多线程下需关闭检查
        if settings.database_url.startswith("sqlite"):
            # 允许不同线程使用同一连接对象（测试与部分部署场景需要）
            connect_args["check_same_thread"] = False
        # 创建 SQLAlchemy 引擎（连接池 + 方言），后续 Session 都挂在此引擎上
        _engine = create_engine(
            # 例如 postgresql+psycopg://... 或测试用的 sqlite+pysqlite:///...
            settings.database_url,
            # 从池中取连接前先 ping，避免拿到已被服务端掐掉的死连接
            pool_pre_ping=True,
            # 启用 SQLAlchemy 2.0 风格 API
            future=True,
            # 把上面拼好的驱动参数传给底层 DBAPI（非 SQLite 时为空 dict）
            connect_args=connect_args,
        )
        # 仅 SQLite：每次真正建立物理连接时执行 PRAGMA
        if settings.database_url.startswith("sqlite"):
            # 注册 connect 事件：新建连接时回调 _set_sqlite_pragma
            @event.listens_for(_engine, "connect")
            def _set_sqlite_pragma(dbapi_connection, _connection_record):  # noqa: ANN001
                # 拿到底层 DBAPI 游标以执行原生 SQL
                cursor = dbapi_connection.cursor()
                # SQLite 默认不强制外键；打开后与 PostgreSQL 行为对齐
                cursor.execute("PRAGMA foreign_keys=ON")
                # 用完关闭游标（连接本身由引擎管理）
                cursor.close()

        # 工厂：调用 factory() 即可得到绑定到上述引擎的 Session 实例
        _SessionLocal = sessionmaker(
            # Session 使用的引擎 / 连接来源
            bind=_engine,
            # 禁止「访问属性时自动 flush」，改由显式 flush/commit 控制写入时机
            autoflush=False,
            # 禁止隐式自动提交；事务边界由 commit() / rollback() 明确控制
            autocommit=False,
            # 提交后不把 ORM 对象属性标为过期，避免路由序列化时再查库触发 DetachedInstanceError
            expire_on_commit=False,
        )
    # 返回已缓存（或刚创建）的引擎实例
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    """返回已绑定引擎的 sessionmaker（副作用：触发引擎初始化）。

    被 app.lifespan 用于启动时初始化引擎；也被 get_db 用于每个请求。
    """
    # 确保 _engine / _SessionLocal 已就绪（首次调用会真正 create_engine）
    get_engine()
    # 类型/逻辑断言：get_engine 成功后 _SessionLocal 必不为 None
    assert _SessionLocal is not None
    # 把工厂交给调用方，由其自行 factory() 开 Session
    return _SessionLocal


def _alembic_config(database_url: str | None = None) -> Config:
    """构造指向项目根 alembic.ini 的 Config，并写入当前 DATABASE_URL。"""
    if not _ALEMBIC_INI.is_file():
        raise FileNotFoundError(f"找不到 Alembic 配置：{_ALEMBIC_INI}")
    cfg = Config(str(_ALEMBIC_INI))
    # 脚本目录相对 ini 已配置；再显式写入 URL，避免 ini 里占位符
    url = database_url or get_settings().database_url
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def migrate_db(database_url: str | None = None) -> None:
    """执行 ``alembic upgrade head``（部署步骤 / CLI；API 启动不会调用）。

    - 空库：新建全部表
    - 已是最新：不变
    - 有未应用迁移：更新表
    """
    # 导入 models，保证 metadata 完整（env.py 也会再 import 一次）
    import deepagents_app.db.models  # noqa: F401

    get_engine()
    cfg = _alembic_config(database_url)
    logger.info("执行 alembic upgrade head")
    command.upgrade(cfg, "head")


def get_db() -> Generator[Session, None, None]:
    """FastAPI Depends：请求成功 commit，异常 rollback；commit 后执行缓存失效。"""
    factory = get_session_factory()
    db = factory()
    try:
        yield db
        db.commit()
        from deepagents_app.services.revisions import flush_cache_invalidations

        flush_cache_invalidations(db)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
