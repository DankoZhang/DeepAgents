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
    from deepagents_app.db.session import reset_engine
    from deepagents_app.services.agent_factory import invalidate_agent_cache

    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{db_path}")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379")
    monkeypatch.setenv("REQUIRE_REDIS_CHECKPOINTER", "false")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    config.get_settings.cache_clear()
    reset_engine()
    invalidate_agent_cache()

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


def test_tool_and_middleware_registry(client):
    tools = client.get("/api/tool/list").json()
    assert len(tools) >= 11
    mws = client.get("/api/middleware/list").json()
    assert {m["name"] for m in mws} >= {
        "LoggingMiddleware",
        "TimingMiddleware",
        "AuditMiddleware",
    }


def test_methodology_crud_and_publish(client):
    created = client.post(
        "/api/methodology",
        json={"name": "SysML方法论", "description": "系统工程", "id": "sysml_methodology"},
    )
    assert created.status_code == 200
    assert created.json()["id"] == "sysml_methodology"
    assert created.json()["status"] == "draft"

    # 无 supervisor 不能发布
    bad = client.post("/api/methodology/sysml_methodology/publish")
    assert bad.status_code == 400

    agent = client.post(
        "/api/agent",
        json={
            "methodology_id": "sysml_methodology",
            "name": "sysml-supervisor",
            "system_prompt": "你是 SysML Supervisor",
            "config": {"role": "supervisor"},
            "middleware_ids": ["mw_logging"],
        },
    )
    assert agent.status_code == 200
    assert agent.json()["config"]["role"] == "supervisor"

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

    # 新会话尚无 checkpoint 历史时，messages 为空列表
    msgs = client.get(f"/api/conversation/{body['thread_id']}/messages")
    assert msgs.status_code == 200
    payload = msgs.json()
    assert payload["thread_id"] == body["thread_id"]
    assert payload["messages"] == []
    assert payload["interrupted"] is False


def test_agent_bind_tools(client):
    # 新建方法论 + subagent，绑定工具
    client.post(
        "/api/methodology",
        json={"name": "绑定测试", "id": "bind_test"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "methodology_id": "bind_test",
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
    """§10 / §14.10：旧会话锁定创建时版本，新会话用最新版。"""
    # 准备可发布方法论
    client.post(
        "/api/methodology",
        json={"name": "版本锁定", "id": "version_lock"},
    )
    client.post(
        "/api/agent",
        json={
            "methodology_id": "version_lock",
            "name": "supervisor",
            "system_prompt": "v1 supervisor",
            "config": {"role": "supervisor"},
        },
    )
    published = client.post("/api/methodology/version_lock/publish")
    assert published.status_code == 200
    v_at_create = published.json()["version"]

    conv_old = client.post(
        "/api/conversation",
        json={"methodology_id": "version_lock"},
    ).json()
    assert conv_old["methodology_version"] == v_at_create

    # 修改 Agent → 方法论 version+1
    agents = client.get("/api/agent/list", params={"methodology_id": "version_lock"}).json()
    supervisor_id = agents[0]["id"]
    updated = client.patch(
        f"/api/agent/{supervisor_id}",
        json={"system_prompt": "v2 supervisor"},
    )
    assert updated.status_code == 200

    meta = client.get("/api/methodology/version_lock").json()
    assert meta["version"] == v_at_create + 1

    # 旧会话仍绑定旧版本；新会话用新版本
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
