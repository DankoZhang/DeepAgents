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


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.db.session import migrate_db, reset_engine
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("REQUIRE_REDIS_CHECKPOINTER", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    config.get_settings.cache_clear()
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
    config.get_settings.cache_clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_seeded_demo_methodology(client):
    r = client.get("/api/methodology/list")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()}
    assert "demo_deepagents" in ids

    detail = client.get("/api/methodology/demo_deepagents")
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
    # 方案 B：种子 Agent 绑定默认模型目录
    assert all(a.get("model_id") == "model_default" for a in body["agents"])
    assert all(a.get("llm_model") and a["llm_model"]["id"] == "model_default" for a in body["agents"])


def test_model_catalog_crud(client):
    listed = client.get("/api/model/list")
    assert listed.status_code == 200
    assert any(m["id"] == "model_default" for m in listed.json())

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
    assert agent.json()["llm_model"]["model_name"] == "deepseek-chat"

    # 仍被引用时不可删
    bad_del = client.delete(f"/api/model/{model_id}")
    assert bad_del.status_code == 400

    # 连通性测试接口可调用（可能因假 key 失败，但应返回结构化结果）
    test_r = client.post("/api/model/test", json={"model_id": model_id})
    assert test_r.status_code == 200
    assert "ok" in test_r.json()
    assert "message" in test_r.json()


def test_tool_and_middleware_registry(client):
    tools = client.get("/api/tool/list").json()
    assert len(tools) >= 11
    assert all(t["tool_type"] == "builtin" for t in tools)
    mws = client.get("/api/middleware/list").json()
    assert {m["name"] for m in mws} >= {
        "LoggingMiddleware",
        "TimingMiddleware",
        "AuditMiddleware",
    }
    # 中间件写接口已下线
    assert client.post("/api/middleware", json={"name": "x", "class_path": "a:b"}).status_code in {
        404,
        405,
    }


def test_create_mcp_tool_only(client):
    bad = client.post(
        "/api/tool",
        json={
            "name": "legacy",
            "class_path": "deepagents_app.tools.qa_tools:save_qa_note",
        },
    )
    assert bad.status_code == 422

    ok = client.post(
        "/api/tool",
        json={
            "name": "demo-mcp",
            "description": "MCP demo",
            "mcp": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "dummy-mcp"],
            },
        },
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["tool_type"] == "mcp"
    assert body["config"]["command"] == "npx"

    # 内置不可删
    bad_del = client.delete("/api/tool/tool_create_document")
    assert bad_del.status_code == 400


def test_global_agent_and_methodology_bind(client):
    created = client.post(
        "/api/methodology",
        json={"name": "SysML方法论", "description": "系统工程", "id": "sysml_methodology"},
    )
    assert created.status_code == 200
    assert created.json()["status"] == "draft"

    bad = client.post("/api/methodology/sysml_methodology/publish")
    assert bad.status_code == 400

    agent = client.post(
        "/api/agent",
        json={
            "name": "sysml-supervisor",
            "system_prompt": "你是 SysML Supervisor",
            "config": {"role": "supervisor"},
            "middleware_ids": ["mw_logging"],
        },
    )
    assert agent.status_code == 200
    assert agent.json()["config"]["role"] == "supervisor"
    agent_id = agent.json()["id"]

    bound = client.post(
        "/api/methodology/sysml_methodology/agents",
        json={"agent_ids": [agent_id], "replace": True},
    )
    assert bound.status_code == 200
    assert {a["id"] for a in bound.json()["agents"]} == {agent_id}

    published = client.post("/api/methodology/sysml_methodology/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "published"

    versions = client.get("/api/methodology/sysml_methodology/versions")
    assert versions.status_code == 200
    assert len(versions.json()) >= 1


def test_conversation_binds_version(client):
    conv = client.post(
        "/api/conversation",
        json={"methodology_id": "demo_deepagents"},
    )
    assert conv.status_code == 200
    body = conv.json()
    assert body["thread_id"]
    assert body["methodology_id"] == "demo_deepagents"
    assert body["methodology_version"] >= 1

    got = client.get(f"/api/conversation/{body['thread_id']}")
    assert got.status_code == 200
    assert got.json()["thread_id"] == body["thread_id"]

    msgs = client.get(f"/api/conversation/{body['thread_id']}/messages")
    assert msgs.status_code == 200
    payload = msgs.json()
    assert payload["thread_id"] == body["thread_id"]
    assert payload["messages"] == []
    assert payload["interrupted"] is False


def test_agent_bind_tools(client):
    agent = client.post(
        "/api/agent",
        json={
            "name": "writer",
            "system_prompt": "写文档",
            "config": {"role": "subagent"},
        },
    ).json()
    bound = client.post(
        f"/api/agent/{agent['id']}/tools",
        json={"tool_ids": ["tool_create_document", "tool_list_documents"]},
    )
    assert bound.status_code == 200
    tool_names = {t["name"] for t in bound.json()["tools"]}
    assert tool_names == {"create_document", "list_documents"}


def test_old_conversation_keeps_methodology_version(client):
    """旧会话锁定创建时版本；改全局 Agent 会 bump 勾选它的方法论。"""
    client.post(
        "/api/methodology",
        json={"name": "版本锁定", "id": "version_lock"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "name": "vl-supervisor",
            "system_prompt": "v1 supervisor",
            "config": {"role": "supervisor"},
        },
    ).json()
    client.post(
        "/api/methodology/version_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    published = client.post("/api/methodology/version_lock/publish")
    assert published.status_code == 200
    v_at_create = published.json()["version"]

    conv_old = client.post(
        "/api/conversation",
        json={"methodology_id": "version_lock"},
    ).json()
    assert conv_old["methodology_version"] == v_at_create

    updated = client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "v2 supervisor"},
    )
    assert updated.status_code == 200

    meta = client.get("/api/methodology/version_lock").json()
    assert meta["version"] == v_at_create + 1

    got_old = client.get(f"/api/conversation/{conv_old['thread_id']}").json()
    assert got_old["methodology_version"] == v_at_create

    conv_new = client.post(
        "/api/conversation",
        json={"methodology_id": "version_lock"},
    ).json()
    assert conv_new["methodology_version"] == meta["version"]
    assert conv_new["methodology_version"] != conv_old["methodology_version"]

    versions = client.get("/api/methodology/version_lock/versions").json()
    assert len(versions) >= 2
