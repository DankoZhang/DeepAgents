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


def test_settings_frozen_and_settings_with():
    from pydantic import ValidationError

    from deepagents_app.config import get_settings, settings_with

    base = get_settings()
    with pytest.raises(ValidationError):
        base.enable_hitl = True  # type: ignore[misc]

    overridden = settings_with(enable_hitl=True)
    assert overridden.enable_hitl is True
    assert get_settings().enable_hitl is False
    assert overridden is not get_settings()


def test_cors_origins_default_explicit(monkeypatch):
    from deepagents_app import config

    monkeypatch.setenv(
        "CORS_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173",
    )
    config.get_settings.cache_clear()
    origins = config.get_settings().cors_origin_list()
    assert origins == [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]
    assert "*" not in origins
    config.get_settings.cache_clear()


def test_mcp_tools_cache_hit(monkeypatch):
    from deepagents_app.db.models import ToolDefinition
    from deepagents_app.registries import tools as tools_reg

    tools_reg.clear_mcp_tools_cache()
    calls = {"n": 0}

    async def fake_aload(tool_def):  # noqa: ANN001
        calls["n"] += 1
        return [object()]

    monkeypatch.setattr(tools_reg, "_aload_mcp_tools", fake_aload)
    row = ToolDefinition(
        id="tool_mcp_cache",
        name="mcp-cache",
        description="",
        tool_type="mcp",
        class_path=None,
        requires_hitl=False,
        config={"transport": "stdio", "command": "echo", "args": []},
        status="active",
    )
    a = tools_reg.load_mcp_tools(row)
    b = tools_reg.load_mcp_tools(row)
    assert calls["n"] == 1
    assert len(a) == 1 and len(b) == 1
    tools_reg.clear_mcp_tools_cache(tool_id=row.id)


def test_seed_dangerous_tools_require_hitl(db_session):
    from deepagents_app.db.models import ToolDefinition

    rows = (
        db_session.query(ToolDefinition)
        .filter(ToolDefinition.owner_user_id == TEST_USER)
        .filter(
            ToolDefinition.name.in_(("run_shell_command", "write_workspace_file"))
        )
        .all()
    )
    assert len(rows) == 2
    assert all(r.requires_hitl for r in rows)


def test_interrupt_tool_names_from_payloads():
    from deepagents_app.registries.tools import interrupt_tool_names_from_payloads

    names = interrupt_tool_names_from_payloads(
        [
            {
                "name": "run_shell_command",
                "tool_type": "builtin",
                "requires_hitl": True,
                "status": "active",
            },
            {
                "name": "list_workspace",
                "tool_type": "builtin",
                "requires_hitl": False,
                "status": "active",
            },
            {
                "name": "mcp-fs",
                "tool_type": "mcp",
                "requires_hitl": True,
                "status": "active",
                "config": {"include_tools": ["read_file", "write_file"]},
            },
        ]
    )
    assert names == {
        "run_shell_command": True,
        "read_file": True,
        "write_file": True,
    }


def test_resolve_interrupt_on_merges_system_and_catalog(monkeypatch):
    from deepagents_app import config
    from deepagents_app.services.agent_factory import _resolve_interrupt_on

    monkeypatch.setenv("ENABLE_HITL", "true")
    config.get_settings.cache_clear()
    settings = config.get_settings()
    merged = _resolve_interrupt_on(
        settings,
        supervisor_config={},
        catalog_interrupt_on={"run_shell_command": True},
    )
    assert merged is not None
    assert merged["run_shell_command"] is True
    assert merged["write_file"] is True
    assert merged["edit_file"] is True
    assert merged["execute"] is True

    monkeypatch.setenv("ENABLE_HITL", "false")
    config.get_settings.cache_clear()
    settings = config.get_settings()
    assert (
        _resolve_interrupt_on(
            settings,
            supervisor_config={},
            catalog_interrupt_on={"run_shell_command": True},
        )
        is None
    )
    config.get_settings.cache_clear()
