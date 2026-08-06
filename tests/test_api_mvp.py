"""
API / 配置库冒烟测试（不依赖 LLM）。

运行::

    python -m pytest tests/test_api_mvp.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TEST_USER = "test-user"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.auth import clear_auth_cache
    from deepagents_app.db.seed import clear_bootstrap_cache
    from deepagents_app.db.session import migrate_db, reset_engine
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("REQUIRE_REDIS_CHECKPOINTER", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("AUTH_DEV_USER_ID", TEST_USER)
    config.get_settings.cache_clear()
    clear_auth_cache()
    clear_bootstrap_cache()
    reset_engine()
    invalidate_agent_cache()

    # 与生产一致：先 migrate，再启动应用（应用 lifespan 不再自动升级 schema）
    migrate_db()

    from fastapi.testclient import TestClient
    from deepagents_app.api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        yield c

    reset_engine()
    invalidate_agent_cache()
    clear_bootstrap_cache()
    clear_auth_cache()
    config.get_settings.cache_clear()


@pytest.fixture()
def demo_ids():
    from deepagents_app.ownership import (
        default_model_id_for_user,
        demo_methodology_id_for_user,
        scoped_id,
    )

    return {
        "user": TEST_USER,
        "methodology": demo_methodology_id_for_user(TEST_USER),
        "model": default_model_id_for_user(TEST_USER),
        "tool_create_document": scoped_id(TEST_USER, "tool_create_document"),
        "tool_list_documents": scoped_id(TEST_USER, "tool_list_documents"),
    }


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_seeded_demo_methodology(client, demo_ids):
    r = client.get("/api/methodology/list")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()}
    assert demo_ids["methodology"] in ids

    detail = client.get(f"/api/methodology/{demo_ids['methodology']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "published"
    names = {a["name"] for a in body["agents"]}
    assert names == {
        "supervisor",
        "document-writer",
        "computer-operator",
        "qa-expert",
    }
    assert all(a.get("model_id") == demo_ids["model"] for a in body["agents"])
    assert all(
        a.get("llm_model") and a["llm_model"]["id"] == demo_ids["model"]
        for a in body["agents"]
    )


def test_user_isolation(client, demo_ids, monkeypatch, tmp_path):
    """用户 A 的资源对用户 B 不可见。"""
    from deepagents_app import config
    from deepagents_app.auth import clear_auth_cache
    from deepagents_app.db.seed import clear_bootstrap_cache
    from deepagents_app.ownership import demo_methodology_id_for_user

    mid_a = demo_ids["methodology"]
    assert client.get(f"/api/methodology/{mid_a}").status_code == 200

    monkeypatch.setenv("AUTH_DEV_USER_ID", "other-user")
    config.get_settings.cache_clear()
    clear_auth_cache()
    clear_bootstrap_cache()

    boot = client.post("/api/bootstrap")
    assert boot.status_code == 200

    listed = client.get("/api/methodology/list")
    assert listed.status_code == 200
    assert mid_a not in {m["id"] for m in listed.json()}
    assert client.get(f"/api/methodology/{mid_a}").status_code == 404
    mid_b = demo_methodology_id_for_user("other-user")
    assert mid_b in {m["id"] for m in listed.json()}


def test_model_catalog_crud(client, demo_ids):
    listed = client.get("/api/model/list")
    assert listed.status_code == 200
    assert any(m["id"] == demo_ids["model"] for m in listed.json())

    created = client.post(
        "/api/model",
        json={
            "name": "测试 DeepSeek",
            "provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 2048,
            "context_length": 128000,
            "api_key": "sk-test",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert body["top_p"] == 0.9
    assert body["context_length"] == 128000
    model_id = body["id"]

    patched = client.patch(
        f"/api/model/{model_id}",
        json={"temperature": 0.5, "clear_api_key": True},
    )
    assert patched.status_code == 200
    assert patched.json()["temperature"] == 0.5
    assert patched.json()["has_api_key"] is False

    agent = client.post(
        "/api/agent",
        json={
            "name": "model-bound-agent",
            "system_prompt": "hi",
            "config": {"role": "subagent"},
            "model_id": model_id,
        },
    )
    assert agent.status_code == 200
    assert agent.json()["model_id"] == model_id
    assert agent.json()["llm_model"]["id"] == model_id

    deleted = client.delete(f"/api/model/{model_id}")
    # 仍被 Agent 引用时应拒绝删除
    assert deleted.status_code == 400

    client.patch(
        f"/api/agent/{agent.json()['id']}",
        json={"model_id": demo_ids["model"]},
    )
    deleted2 = client.delete(f"/api/model/{model_id}")
    assert deleted2.status_code == 200
    refreshed = client.get(f"/api/agent/{agent.json()['id']}")
    assert refreshed.status_code == 200
    assert refreshed.json()["model_id"] == demo_ids["model"]


def test_skill_crud_and_agent_bind(client):
    created = client.post(
        "/api/skill",
        json={
            "name": "my-skill",
            "description": "demo",
            "content": "Do the skill carefully.",
        },
    )
    assert created.status_code == 200
    skill = created.json()
    assert skill["name"] == "my-skill"
    assert "Do the skill carefully" in skill["content"]

    agent = client.post(
        "/api/agent",
        json={
            "name": "skill-agent",
            "system_prompt": "hi",
            "config": {"role": "subagent"},
            "skill_ids": [skill["id"]],
        },
    )
    assert agent.status_code == 200
    assert {s["id"] for s in agent.json()["skills"]} == {skill["id"]}

    listed = client.get("/api/skill/list")
    assert listed.status_code == 200
    assert any(s["id"] == skill["id"] for s in listed.json())


def test_mcp_tool_create_and_guardrails(client, demo_ids):
    created = client.post(
        "/api/tool",
        json={
            "name": "demo-mcp",
            "description": "mcp demo",
            "mcp": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "demo"],
            },
        },
    )
    assert created.status_code == 200
    tool = created.json()
    assert tool["tool_type"] == "mcp"
    assert tool["config"]["command"] == "npx"

    bad_del = client.delete(f"/api/tool/{demo_ids['tool_create_document']}")
    assert bad_del.status_code == 400


def test_create_conversation_and_list(client, demo_ids):
    created = client.post(
        "/api/conversation",
        json={"methodology_id": demo_ids["methodology"]},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["methodology_id"] == demo_ids["methodology"]
    assert body["user_id"] == TEST_USER
    assert body["thread_id"]

    listed = client.get("/api/conversation/list")
    assert listed.status_code == 200
    assert any(c["thread_id"] == body["thread_id"] for c in listed.json())

    msgs = client.get(f"/api/conversation/{body['thread_id']}/messages")
    assert msgs.status_code == 200
    assert msgs.json()["messages"] == []


def test_agent_bind_tools(client, demo_ids):
    agent = client.post(
        "/api/agent",
        json={
            "name": "bind-tools-agent",
            "system_prompt": "hi",
            "config": {"role": "subagent"},
        },
    ).json()
    bound = client.post(
        f"/api/agent/{agent['id']}/tools",
        json={
            "tool_ids": [
                demo_ids["tool_create_document"],
                demo_ids["tool_list_documents"],
            ]
        },
    )
    assert bound.status_code == 200
    ids = {t["id"] for t in bound.json()["tools"]}
    assert demo_ids["tool_create_document"] in ids
    assert demo_ids["tool_list_documents"] in ids


def test_methodology_publish_and_version_lock(client):
    client.post(
        "/api/methodology",
        json={"name": "版本锁定", "id": "version_lock"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "name": "vl-supervisor",
            "system_prompt": "supervisor",
            "config": {"role": "supervisor"},
        },
    ).json()
    client.post(
        "/api/methodology/version_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    published = client.post("/api/methodology/version_lock/publish")
    assert published.status_code == 200
    v1 = published.json()["version"]

    conv = client.post(
        "/api/conversation",
        json={"methodology_id": "version_lock"},
    ).json()
    assert conv["methodology_version"] == v1

    client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "supervisor v2"},
    )
    meta = client.get("/api/methodology/version_lock").json()
    assert meta["version"] > v1
    # 旧会话仍锁定 v1
    detail = client.get(f"/api/conversation/{conv['thread_id']}")
    assert detail.json()["methodology_version"] == v1

    versions = client.get("/api/methodology/version_lock/versions").json()
    assert len(versions) >= 2


def test_snapshot_locks_skill_and_tool_payloads(client, demo_ids):
    """快照内嵌 Skill/Tool 正文与配置；目录后续修改不影响旧版本重建。"""
    from deepagents_app.db.models import ToolDefinition
    from deepagents_app.db.session import get_session_factory
    from deepagents_app.services.agent_factory import _resolve_runtime_bindings
    from deepagents_app.services.revisions import get_revision

    skill = client.post(
        "/api/skill",
        json={
            "name": "lock-skill",
            "description": "v1 desc",
            "content": "skill body v1",
        },
    ).json()
    mcp = client.post(
        "/api/tool",
        json={
            "name": "lock-mcp",
            "description": "mcp v1",
            "mcp": {
                "transport": "stdio",
                "command": "echo",
                "args": ["v1"],
            },
        },
    ).json()

    client.post("/api/methodology", json={"name": "payload锁定", "id": "payload_lock"})
    agent = client.post(
        "/api/agent",
        json={
            "name": "pl-supervisor",
            "system_prompt": "supervisor",
            "config": {"role": "supervisor"},
            "skill_ids": [skill["id"]],
            "tool_ids": [mcp["id"], demo_ids["tool_create_document"]],
        },
    ).json()
    client.post(
        "/api/methodology/payload_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    published = client.post("/api/methodology/payload_lock/publish")
    assert published.status_code == 200
    v1 = published.json()["version"]

    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        rev_v1 = get_revision(db, "payload_lock", v1)
        assert rev_v1 is not None
        snap_agent = rev_v1.snapshot["agents"][0]
        assert "skill body v1" in snap_agent["skills"][0]["content"]
        mcp_snap = next(t for t in snap_agent["tools"] if t["id"] == mcp["id"])
        assert mcp_snap["config"]["args"] == ["v1"]
        assert mcp["id"] in snap_agent["tool_ids"]
        assert skill["id"] in snap_agent["skill_ids"]
        assert any(
            t["id"] == demo_ids["tool_create_document"] for t in snap_agent["tools"]
        )
        assert "middlewares" in snap_agent
        assert "skills" in snap_agent
        assert "tools" in snap_agent

    patched_skill = client.patch(
        f"/api/skill/{skill['id']}",
        json={"content": "skill body v2"},
    )
    assert patched_skill.status_code == 200
    assert "skill body v2" in patched_skill.json()["content"]

    patched_mcp = client.patch(
        f"/api/tool/{mcp['id']}",
        json={"mcp": {"transport": "stdio", "command": "echo", "args": ["v2"]}},
    )
    assert patched_mcp.status_code == 200
    assert patched_mcp.json()["config"]["args"] == ["v2"]

    meta = client.get("/api/methodology/payload_lock").json()
    assert meta["version"] > v1

    with SessionLocal() as db:
        rev_v1 = get_revision(db, "payload_lock", v1)
        snap_agent = rev_v1.snapshot["agents"][0]
        assert "skill body v1" in snap_agent["skills"][0]["content"]
        assert "skill body v2" not in snap_agent["skills"][0]["content"]
        mcp_snap = next(t for t in snap_agent["tools"] if t["id"] == mcp["id"])
        assert mcp_snap["config"]["args"] == ["v1"]

        live_mcp = db.get(ToolDefinition, mcp["id"])
        assert live_mcp is not None
        assert live_mcp.config["args"] == ["v2"]

        resolve_agent = {
            **snap_agent,
            "tools": [t for t in snap_agent["tools"] if t["tool_type"] != "mcp"],
            "tool_ids": [
                tid for tid in snap_agent["tool_ids"] if tid != mcp["id"]
            ],
        }
        _, _, _, _, tools, _, skill_rows = _resolve_runtime_bindings(resolve_agent)
        assert "skill body v1" in skill_rows[0].content
        assert any(getattr(t, "name", None) == "create_document" for t in tools)
