"""共享 pytest fixtures。"""

from __future__ import annotations

import logging

import pytest

TEST_USER = "test-user"
SVC_TEST_USER = "svc-test-user"
# AsyncRedisSaver / RediSearch 要求 index 建在 DB 0；用测试用户前缀隔离并清理
TEST_REDIS_URL = "redis://localhost:6379"

logger = logging.getLogger(__name__)


def _cleanup_test_redis_keys() -> None:
    """
    删除本机 Redis 中测试用户相关 key，避免旧 checkpoint 污染后续用例。

    不 ``FLUSHDB``：开发会话与测试共用 DB 0（RediSearch 限制）。
    """
    try:
        import redis

        client = redis.Redis.from_url(
            TEST_REDIS_URL,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        try:
            deleted = 0
            for owner in (TEST_USER, SVC_TEST_USER):
                # checkpoint / write_keys 等键都带 user_id 前缀
                for key in client.scan_iter(match=f"*{owner}*", count=500):
                    client.delete(key)
                    deleted += 1
            if deleted:
                logger.info("已清理测试 Redis key %s 条", deleted)
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理测试 Redis 失败（%s）: %s", TEST_REDIS_URL, exc)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.auth import clear_auth_cache
    from deepagents_app.db.seed import clear_bootstrap_cache
    from deepagents_app.db.session import migrate_db, reset_engine
    from deepagents_app.services.runtime.agent_factory import invalidate_agent_cache

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("AUTH_DEV_USER_ID", TEST_USER)
    monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_STDIO_COMMAND_ALLOWLIST", "npx,uvx,node,python,python3,echo"
    )
    monkeypatch.setenv("SECRETS_ALLOW_INSECURE_DEV_KEY", "true")
    # 测试里禁用后台 GC，避免干扰断言 / 占用事件循环
    monkeypatch.setenv("SKILLS_GC_INTERVAL_HOURS", "0")
    monkeypatch.setenv("CONTENT_BLOB_GC_INTERVAL_HOURS", "0")
    config.get_settings.cache_clear()
    clear_auth_cache()
    clear_bootstrap_cache()
    reset_engine()
    invalidate_agent_cache()
    _cleanup_test_redis_keys()

    migrate_db()

    from fastapi.testclient import TestClient
    from deepagents_app.api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        boot = c.post("/api/bootstrap")
        assert boot.status_code == 200, boot.text
        yield c

    reset_engine()
    invalidate_agent_cache()
    clear_bootstrap_cache()
    clear_auth_cache()
    config.get_settings.cache_clear()
    _cleanup_test_redis_keys()


@pytest.fixture()
async def db_session(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.auth import clear_auth_cache
    from deepagents_app.db.seed import clear_bootstrap_cache, ensure_user_bootstrap
    from deepagents_app.db.session import (
        get_async_session_factory,
        migrate_db,
        reset_engine,
    )
    from deepagents_app.services.runtime.agent_factory import invalidate_agent_cache
    from deepagents_app.services.versioning.revisions import flush_cache_invalidations

    db_path = tmp_path / "svc.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", TEST_REDIS_URL)
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("AUTH_DEV_USER_ID", SVC_TEST_USER)
    monkeypatch.setenv("AGENT_CACHE_MAX_SIZE", "2")
    monkeypatch.setenv("MCP_STDIO_ENABLED", "true")
    monkeypatch.setenv(
        "MCP_STDIO_COMMAND_ALLOWLIST", "npx,uvx,node,python,python3,echo"
    )
    monkeypatch.setenv("SECRETS_ALLOW_INSECURE_DEV_KEY", "true")
    monkeypatch.setenv("SKILLS_GC_INTERVAL_HOURS", "0")
    monkeypatch.setenv("CONTENT_BLOB_GC_INTERVAL_HOURS", "0")
    config.get_settings.cache_clear()
    clear_auth_cache()
    clear_bootstrap_cache()
    reset_engine()
    invalidate_agent_cache()
    _cleanup_test_redis_keys()
    migrate_db()

    factory = get_async_session_factory()
    db = factory()
    try:
        await ensure_user_bootstrap(db, SVC_TEST_USER)
        await db.commit()
        flush_cache_invalidations(db)
        yield db
    finally:
        await db.close()
        reset_engine()
        invalidate_agent_cache()
        clear_bootstrap_cache()
        clear_auth_cache()
        config.get_settings.cache_clear()
        _cleanup_test_redis_keys()
