"""
services 层单测（不依赖 LLM / 不经 HTTP）。

运行::

    python -m pytest tests/test_services_unit.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_USER = "svc-test-user"


@pytest.fixture()
def db_session(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.auth import clear_auth_cache
    from deepagents_app.db.seed import clear_bootstrap_cache, ensure_user_bootstrap
    from deepagents_app.db.session import get_session_factory, migrate_db, reset_engine
    from deepagents_app.services.agent_factory import invalidate_agent_cache
    from deepagents_app.services.revisions import flush_cache_invalidations

    db_path = tmp_path / "svc.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("REQUIRE_REDIS_CHECKPOINTER", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("AUTH_DEV_USER_ID", TEST_USER)
    monkeypatch.setenv("AGENT_CACHE_MAX_SIZE", "2")
    config.get_settings.cache_clear()
    clear_auth_cache()
    clear_bootstrap_cache()
    reset_engine()
    invalidate_agent_cache()
    migrate_db()

    factory = get_session_factory()
    db = factory()
    try:
        ensure_user_bootstrap(db, TEST_USER)
        db.commit()
        flush_cache_invalidations(db)
        yield db
    finally:
        db.close()
        reset_engine()
        invalidate_agent_cache()
        clear_bootstrap_cache()
        clear_auth_cache()
        config.get_settings.cache_clear()


def test_validate_resource_id_rejects_path_traversal():
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.ownership import validate_resource_id

    with pytest.raises(BusinessError):
        validate_resource_id("../../../../PWNED")
    with pytest.raises(BusinessError):
        validate_resource_id("bad/id")
    validate_resource_id("ok_agent-1")


def test_list_methodologies_pagination(db_session):
    from deepagents_app.services import methodology as methodology_svc

    rows, total = methodology_svc.list_methodologies(
        db_session, owner_user_id=TEST_USER, limit=1, offset=0
    )
    assert total >= 1
    assert len(rows) == 1

    rows2, total2 = methodology_svc.list_methodologies(
        db_session, owner_user_id=TEST_USER, limit=1, offset=1
    )
    assert total2 == total
    if total > 1:
        assert rows[0].id != rows2[0].id


def test_create_methodology_rejects_bad_id(db_session):
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.services import methodology as methodology_svc

    with pytest.raises(BusinessError):
        methodology_svc.create_methodology(
            db_session,
            owner_user_id=TEST_USER,
            name="evil",
            methodology_id="../escape",
        )


def test_agent_cache_lru_eviction(db_session, monkeypatch, tmp_path):
    from deepagents_app import config
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services import agent_factory as af

    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert settings.agent_cache_max_size == 2

    mid = demo_methodology_id_for_user(TEST_USER)
    af.invalidate_agent_cache()

    # 不真正 create_deep_agent：用假对象测 LRU 读写
    af._cache.clear()
    af._cache_put("u:a:v1", object())
    af._cache_put("u:b:v1", object())
    assert len(af._cache) == 2
    # 访问 a，再放入 c → 应淘汰最久未用的 b
    assert af._cache_get("u:a:v1") is not None
    evicted = af._cache_put("u:c:v1", object())
    assert "u:b:v1" in evicted
    assert "u:b:v1" not in af._cache
    assert "u:a:v1" in af._cache
    assert "u:c:v1" in af._cache
    # mid 仅用于确认 bootstrap 可用
    assert mid


def test_bind_agents_returns_detail(db_session):
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services import agents as agents_svc
    from deepagents_app.services import methodology as methodology_svc

    mid = demo_methodology_id_for_user(TEST_USER)
    agents, _ = agents_svc.list_agents(db_session, owner_user_id=TEST_USER, limit=10)
    assert agents
    # 草稿方法论：绑定不升版
    draft = methodology_svc.create_methodology(
        db_session,
        owner_user_id=TEST_USER,
        name="svc-draft-m",
        agent_ids=[agents[0].id],
    )
    assert draft is not None
    assert draft.version == 1
    assert len(draft.agents) == 1
