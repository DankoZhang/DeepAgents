"""Alembic 运行环境：URL 与 metadata 来自应用配置 / ORM。"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 确保 ORM 表注册进 Base.metadata（autogenerate / upgrade 都需要）
import deepagents_app.db.models  # noqa: F401
from deepagents_app.config import get_settings
from deepagents_app.db.base import Base

config = context.config

if config.config_file_name is not None:
    # 不覆盖应用已配置的 logger（启动时由 FastAPI lifespan 初始化）
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _database_url() -> str:
    """优先用程序注入的 URL，否则读应用 Settings（.env / 环境变量）。"""
    url = config.get_main_option("sqlalchemy.url")
    if url and url.strip() and "driver://" not in url:
        return url
    return get_settings().database_url


def run_migrations_offline() -> None:
    """离线模式：只生成 SQL，不连库。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=True,  # SQLite 改表需要 batch
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    configuration = config.get_section(config.config_ini_section, {}) or {}
    configuration["sqlalchemy.url"] = _database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
