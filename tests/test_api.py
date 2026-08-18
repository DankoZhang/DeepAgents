#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   test_api.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   test_api.py

API / 配置库冒烟测试（不依赖 LLM）。

运行::

    python -m pytest tests/test_api.py -q
"""

from __future__ import annotations

import pytest

from tests.conftest import TEST_USER


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
        "tool_search_knowledge": scoped_id(TEST_USER, "tool_search_knowledge"),
        "tool_list_knowledge_topics": scoped_id(
            TEST_USER, "tool_list_knowledge_topics"
        ),
    }


def test_health(client):
    r = client.get("/health")
    body = r.json()
    assert "checks" in body
    assert "db" in body["checks"]
    assert "redis" in body["checks"]
    # 测试库必须可用；Redis 不可用时整体为 error/503（无内存降级）
    assert body["checks"]["db"] == "ok"
    if body["checks"]["redis"] == "ok":
        assert body["status"] == "ok"
        assert r.status_code == 200
    else:
        assert body["status"] == "error"
        assert r.status_code == 503


def test_seeded_demo_methodology(client, demo_ids):
    r = client.get("/api/methodology/list")
    assert r.status_code == 200
    ids = {m["id"] for m in r.json()}
    assert demo_ids["methodology"] in ids

    detail = client.get(f"/api/methodology/{demo_ids['methodology']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["status"] == "published"
    assert body["name"] == "supervisor"
    names = {a["name"] for a in body["agents"]}
    assert names == {
        "supervisor",
        "qa-expert",
    }
    assert all(a.get("enabled") is True for a in body["agents"])
    supervisor = next(a for a in body["agents"] if a["name"] == "supervisor")
    assert supervisor.get("methodology_id") == demo_ids["methodology"]
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
    seed = next(m for m in listed.json() if m["id"] == demo_ids["model"])
    assert seed["is_default"] is True

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
            "api_key": "sk-test",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert body["top_p"] == 0.9
    assert "context_length" not in body
    assert body["is_default"] is False
    model_id = body["id"]

    patched = client.patch(
        f"/api/model/{model_id}",
        json={"temperature": 0.5},
    )
    assert patched.status_code == 200
    assert patched.json()["temperature"] == 0.5
    assert patched.json()["has_api_key"] is True

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


def test_model_default_flag_and_agent_fallback(client, demo_ids):
    """种子模型为默认；切换默认后，未指定 model_id 的 Agent 绑定新默认。"""
    listed = client.get("/api/model/list")
    assert listed.status_code == 200
    seed = next(m for m in listed.json() if m["id"] == demo_ids["model"])
    assert seed["is_default"] is True

    created = client.post(
        "/api/model",
        json={
            "name": "备用默认模型",
            "provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test-default",
        },
    )
    assert created.status_code == 200
    new_id = created.json()["id"]
    assert created.json()["is_default"] is False

    switched = client.patch(f"/api/model/{new_id}", json={"is_default": True})
    assert switched.status_code == 200
    assert switched.json()["is_default"] is True
    seed_after = client.get(f"/api/model/{demo_ids['model']}")
    assert seed_after.status_code == 200
    assert seed_after.json()["is_default"] is False

    listed2 = client.get("/api/model/list")
    assert sum(1 for m in listed2.json() if m["is_default"]) == 1

    agent = client.post(
        "/api/agent",
        json={"name": "fallback-to-new-default", "system_prompt": "hi"},
    )
    assert agent.status_code == 200
    assert agent.json()["model_id"] == new_id

    client.patch(f"/api/model/{demo_ids['model']}", json={"is_default": True})
    agent2 = client.post(
        "/api/agent",
        json={"name": "fallback-to-seed-default", "system_prompt": "hi"},
    )
    assert agent2.status_code == 200
    assert agent2.json()["model_id"] == demo_ids["model"]

    create_as_default = client.post(
        "/api/model",
        json={
            "name": "创建时即默认",
            "provider": "openai_compatible",
            "model_name": "deepseek-chat",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "sk-test-create-default",
            "is_default": True,
        },
    )
    assert create_as_default.status_code == 200
    assert create_as_default.json()["is_default"] is True
    listed3 = client.get("/api/model/list")
    defaults = [m for m in listed3.json() if m["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["id"] == create_as_default.json()["id"]


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
    assert skill.get("files") == {}

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

    bad_del = client.delete(f"/api/tool/{demo_ids['tool_search_knowledge']}")
    assert bad_del.status_code == 400


def test_http_tool_create_update_and_expand(client):
    created = client.post(
        "/api/tool",
        json={
            "name": "get_weather",
            "description": "查询城市天气",
            "tool_type": "http",
            "http": {
                "method": "GET",
                "url": "http://127.0.0.1:9/weather/{city}",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名"},
                        "units": {
                            "type": "string",
                            "enum": ["metric", "imperial"],
                            "description": "单位",
                        },
                    },
                    "required": ["city"],
                },
                "headers": {"Authorization": "Bearer test"},
            },
        },
    )
    assert created.status_code == 200, created.text
    tool = created.json()
    assert tool["tool_type"] == "http"
    assert tool["requires_hitl"] is False
    assert tool["config"]["method"] == "GET"
    assert tool["config"]["param_in"]["city"] == "path"
    assert tool["config"]["param_in"]["units"] == "query"
    assert tool["config"]["headers"]["Authorization"] == "Bearer test"

    listed = client.get("/api/tool/list", params={"tool_type": "http"})
    assert listed.status_code == 200
    assert any(t["id"] == tool["id"] for t in listed.json())

    patched = client.patch(
        f"/api/tool/{tool['id']}",
        json={
            "http": {
                "method": "GET",
                "url": "http://127.0.0.1:9/v2/weather",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "城市名"},
                    },
                    "required": ["city"],
                },
            }
        },
    )
    assert patched.status_code == 200
    assert patched.json()["config"]["url"] == "http://127.0.0.1:9/v2/weather"

    mcp_on_http = client.patch(
        f"/api/tool/{tool['id']}",
        json={"mcp": {"transport": "stdio", "command": "npx"}},
    )
    assert mcp_on_http.status_code == 400

    both = client.post(
        "/api/tool",
        json={
            "name": "bad-both",
            "mcp": {"transport": "stdio", "command": "npx"},
            "http": {
                "url": "http://127.0.0.1:9/x",
                "input_schema": {"type": "object", "properties": {}},
            },
        },
    )
    assert both.status_code == 422

    deleted = client.delete(f"/api/tool/{tool['id']}")
    assert deleted.status_code == 200


def test_http_tool_rejects_host_placeholder(client):
    created = client.post(
        "/api/tool",
        json={
            "name": "evil-host",
            "http": {
                "url": "http://{host}/weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"host": {"type": "string"}},
                },
            },
        },
    )
    assert created.status_code == 400


def test_tool_connectivity_endpoints(client, demo_ids, monkeypatch):
    builtin = client.post(f"/api/tool/{demo_ids['tool_search_knowledge']}/test")
    assert builtin.status_code == 200
    assert builtin.json()["ok"] is False
    assert "内置" in builtin.json()["message"]

    class _Resp:
        status_code = 204
        text = ""

    class _Client:
        async def request(self, method, url, **kwargs):  # noqa: ANN001, ARG002
            assert method == "GET"
            assert "ping" in url
            return _Resp()

    monkeypatch.setattr(
        "deepagents_app.registries.http_tools.get_http_tool_client",
        lambda: _Client(),
    )
    inline = client.post(
        "/api/tool/test",
        json={
            "tool_type": "http",
            "http": {
                "method": "POST",
                "url": "http://127.0.0.1:9/orders/{id}",
                "input_schema": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
    )
    assert inline.status_code == 200, inline.text
    body = inline.json()
    assert body["ok"] is True
    assert "HTTP 204" in body["detail"]
    assert "GET" in body["detail"]


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
                demo_ids["tool_search_knowledge"],
                demo_ids["tool_list_knowledge_topics"],
            ]
        },
    )
    assert bound.status_code == 200
    ids = {t["id"] for t in bound.json()["tools"]}
    assert demo_ids["tool_search_knowledge"] in ids
    assert demo_ids["tool_list_knowledge_topics"] in ids


def test_methodology_publish_and_version_lock(client):
    client.post(
        "/api/methodology",
        json={"name": "vl-supervisor", "id": "version_lock"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "name": "vl-supervisor",
            "system_prompt": "supervisor",
            "config": {"role": "supervisor", "methodology_id": "version_lock"},
        },
    ).json()
    client.post(
        "/api/methodology/version_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    enabled = client.post(f"/api/agent/{agent['id']}/enable")
    assert enabled.status_code == 200, enabled.text
    v1 = client.get("/api/methodology/version_lock").json()["version"]

    conv = client.post(
        "/api/conversation",
        json={"methodology_id": "version_lock"},
    ).json()
    assert conv["methodology_version"] == v1

    disabled = client.post(f"/api/agent/{agent['id']}/disable")
    assert disabled.status_code == 200
    patched = client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "supervisor v2"},
    )
    assert patched.status_code == 200
    reenabled = client.post(f"/api/agent/{agent['id']}/enable")
    assert reenabled.status_code == 200
    meta = client.get("/api/methodology/version_lock").json()
    assert meta["version"] > v1
    # 旧会话仍锁定 v1
    detail = client.get(f"/api/conversation/{conv['thread_id']}")
    assert detail.json()["methodology_version"] == v1

    versions = client.get("/api/methodology/version_lock/versions").json()
    assert len(versions) >= 2


def test_draft_agent_edit_does_not_bump_version(client):
    """草稿（未启用）时改 Agent 不升版；停用后编辑再启用才升版。"""
    client.post(
        "/api/methodology",
        json={"name": "da-supervisor", "id": "draft_accum"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "name": "da-supervisor",
            "system_prompt": "v1",
            "config": {"role": "supervisor", "methodology_id": "draft_accum"},
        },
    ).json()
    client.post(
        "/api/methodology/draft_accum/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    before = client.get("/api/methodology/draft_accum").json()
    assert before["status"] == "draft"
    v0 = before["version"]

    client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "v2-draft"},
    )
    after = client.get("/api/methodology/draft_accum").json()
    assert after["version"] == v0

    enabled = client.post(f"/api/agent/{agent['id']}/enable")
    assert enabled.status_code == 200, enabled.text
    published = client.get("/api/methodology/draft_accum").json()
    assert published["status"] == "published"
    assert published["version"] == v0

    client.post(f"/api/agent/{agent['id']}/disable")
    patched = client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "v3-published"},
    )
    assert patched.status_code == 200
    reenabled = client.post(f"/api/agent/{agent['id']}/enable")
    assert reenabled.status_code == 200
    bumped = client.get("/api/methodology/draft_accum").json()
    assert bumped["version"] > v0


def test_agent_cache_lru_evicts_and_drops_build_lock(tmp_path, monkeypatch):
    """缓存超额时按 LRU 淘汰，并释放对应构建锁。"""
    from deepagents_app import config
    from deepagents_app.services.runtime import agent_factory as af
    monkeypatch.setenv("AGENT_CACHE_MAX_SIZE", "2")
    monkeypatch.setenv("WORKSPACE_DIR", str(tmp_path / "workspace"))
    config.get_settings.cache_clear()
    af.invalidate_agent_cache()

    af._cache_put("m1:v1", "A")
    af._build_lock_for("m1:v1")
    af._cache_put("m2:v1", "B")
    af._build_lock_for("m2:v1")
    evicted = af._cache_put("m3:v1", "C")
    assert evicted == ["m1:v1"]
    for key in evicted:
        af._drop_build_lock(key)
    assert "m1:v1" not in af._cache
    assert "m1:v1" not in af._build_locks
    assert list(af._cache.keys()) == ["m2:v1", "m3:v1"]
    af.invalidate_agent_cache()
    config.get_settings.cache_clear()


def test_revision_prune_keeps_referenced_versions(client, monkeypatch):
    """升版后裁剪历史；会话锁定版本与 live 当前版不会被删。"""
    from deepagents_app import config

    monkeypatch.setenv("METHODOLOGY_REVISION_KEEP", "1")
    config.get_settings.cache_clear()

    client.post("/api/methodology", json={"name": "prune-sup", "id": "prune_meth"})
    agent = client.post(
        "/api/agent",
        json={
            "name": "prune-sup",
            "system_prompt": "s0",
            "config": {"role": "supervisor", "methodology_id": "prune_meth"},
        },
    ).json()
    client.post(
        "/api/methodology/prune_meth/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    enabled = client.post(f"/api/agent/{agent['id']}/enable")
    assert enabled.status_code == 200, enabled.text
    v_pub = client.get("/api/methodology/prune_meth").json()["version"]
    conv = client.post(
        "/api/conversation",
        json={"methodology_id": "prune_meth"},
    ).json()
    assert conv["methodology_version"] == v_pub

    for i in range(4):
        assert client.post(f"/api/agent/{agent['id']}/disable").status_code == 200
        assert client.patch(
            f"/api/agent/{agent['id']}",
            json={"system_prompt": f"s{i+1}"},
        ).status_code == 200
        assert client.post(f"/api/agent/{agent['id']}/enable").status_code == 200

    versions = client.get("/api/methodology/prune_meth/versions").json()
    version_nums = {v["version"] for v in versions}
    live = client.get("/api/methodology/prune_meth").json()["version"]
    # 会话锁定版 + live 版必须保留；其余按 keep=1 裁剪
    assert v_pub in version_nums
    assert live in version_nums
    assert len(versions) <= 3  # referenced + live + at most 1 recent extra
    config.get_settings.cache_clear()


def test_delete_conversation_clears_checkpointer(client):
    """删除会话后元数据清除，同 thread_id 可再次创建。"""
    from deepagents_app.ownership import demo_methodology_id_for_user

    mid = demo_methodology_id_for_user(TEST_USER)
    created = client.post(
        "/api/conversation",
        json={"methodology_id": mid, "thread_id": "del_cp_thread_1"},
    ).json()
    tid = created["thread_id"]

    deleted = client.delete(f"/api/conversation/{tid}")
    assert deleted.status_code == 200
    assert client.get(f"/api/conversation/{tid}").status_code == 404

    again = client.post(
        "/api/conversation",
        json={"methodology_id": mid, "thread_id": tid},
    )
    assert again.status_code == 200
    assert again.json()["thread_id"] == tid


def test_snapshot_locks_skill_and_tool_payloads(client, demo_ids):
    """快照内嵌 Skill/Tool 正文与配置；目录后续修改不影响旧版本重建。"""
    import asyncio

    from deepagents_app.db.models import ToolDefinition
    from deepagents_app.db.session import get_async_session_factory
    from deepagents_app.services.runtime.agent_factory import _resolve_runtime_bindings
    from deepagents_app.services.versioning.revisions import get_revision

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
            "config": {"role": "supervisor", "enabled": True},
            "skill_ids": [skill["id"]],
            "tool_ids": [mcp["id"], demo_ids["tool_search_knowledge"]],
        },
    ).json()
    client.post(
        "/api/methodology/payload_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    published = client.post("/api/methodology/payload_lock/publish")
    assert published.status_code == 200
    v1 = published.json()["version"]

    async def _read_rev(version: int):
        async with get_async_session_factory()() as db:
            return await get_revision(db, "payload_lock", version)

    rev_v1 = asyncio.run(_read_rev(v1))
    assert rev_v1 is not None
    snap_agent = rev_v1.snapshot["agents"][0]
    skill_snap = snap_agent["skills"][0]
    assert "content_hash" in skill_snap
    assert "content" not in skill_snap
    assert skill_snap.get("files") == []
    from deepagents_app.services.versioning.content_blobs import hydrate_snapshot_content

    async def _hydrate():
        async with get_async_session_factory()() as db:
            hydrated = await hydrate_snapshot_content(db, rev_v1.snapshot)
            return hydrated["agents"][0]

    hydrated_agent = asyncio.run(_hydrate())
    assert "skill body v1" in hydrated_agent["skills"][0]["content"]
    mcp_snap = next(t for t in snap_agent["tools"] if t["id"] == mcp["id"])
    assert mcp_snap["config"]["args"] == ["v1"]
    assert any(t["id"] == mcp["id"] for t in snap_agent["tools"])
    assert any(s["id"] == skill["id"] for s in snap_agent["skills"])
    assert "tool_ids" not in snap_agent
    assert "skill_ids" not in snap_agent
    assert "middleware_ids" not in snap_agent
    assert any(
        t["id"] == demo_ids["tool_search_knowledge"] for t in snap_agent["tools"]
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

    async def _assert_locked_and_live():
        async with get_async_session_factory()() as db:
            rev = await get_revision(db, "payload_lock", v1)
            snap = rev.snapshot["agents"][0]
            hydrated = await hydrate_snapshot_content(db, rev.snapshot)
            h_skills = hydrated["agents"][0]["skills"]
            assert "skill body v1" in h_skills[0]["content"]
            assert "skill body v2" not in h_skills[0]["content"]
            snap = hydrated["agents"][0]
            mcp_locked = next(t for t in snap["tools"] if t["id"] == mcp["id"])
            assert mcp_locked["config"]["args"] == ["v1"]

            live_mcp = await db.get(ToolDefinition, mcp["id"])
            assert live_mcp is not None
            assert live_mcp.config["args"] == ["v2"]
            return snap

    snap_agent = asyncio.run(_assert_locked_and_live())
    resolve_agent = {
        **snap_agent,
        "tools": [t for t in snap_agent["tools"] if t["tool_type"] != "mcp"],
    }
    _, _, _, _, tools, _, skill_rows = asyncio.run(
        _resolve_runtime_bindings(resolve_agent)
    )
    assert "skill body v1" in skill_rows[0].content
    assert any(getattr(t, "name", None) == "search_knowledge" for t in tools)


def test_reject_path_traversal_ids(client):
    """客户端自带主键含路径穿越字符时拒绝创建。"""
    bad = client.post(
        "/api/methodology",
        json={"name": "pwn", "id": "../../../../PWNED"},
    )
    assert bad.status_code == 400

    bad_agent = client.post(
        "/api/agent",
        json={
            "name": "pwn-sup",
            "id": "../../PWNAGENT",
            "system_prompt": "x",
            "config": {"role": "supervisor", "enabled": True},
        },
    )
    assert bad_agent.status_code == 400

    ok = client.post(
        "/api/methodology",
        json={"name": "safe-id", "id": "safe_methodology_1"},
    )
    assert ok.status_code == 200
    assert ok.json()["id"] == "safe_methodology_1"


def test_skills_materialize_rejects_escaped_scope(tmp_path, monkeypatch):
    """agent_id / scope 含穿越时不得落到 workspace 之外。"""
    from deepagents_app import config
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.config import Settings
    from deepagents_app.services.catalog.skills import (
        _safe_materialize_root,
        materialize_agent_skills,
    )

    ws = tmp_path / "workspace"
    ws.mkdir()
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    with pytest.raises(BusinessError):
        _safe_materialize_root(settings, scope="../../../../outside")
    with pytest.raises(BusinessError):
        materialize_agent_skills(settings, "../../PWNAGENT", [])
    with pytest.raises(BusinessError):
        _safe_materialize_root(
            settings, scope="../../evil", agent_id="agent_ok"
        )
    root = _safe_materialize_root(
        settings, scope="abcd1234abcd1234", agent_id="agent_ok"
    )
    assert root.is_relative_to((ws / "skills").resolve())
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "evil").exists()
    assert not (ws.parent / "PWNAGENT").exists()


def test_skills_materialize_reuses_complete_dir(tmp_path, monkeypatch):
    """同内容指纹二次物化应复用目录，不重写。"""
    from deepagents_app import config
    from deepagents_app.config import Settings
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.services.catalog.skills import (
        materialize_agent_skills,
        skills_fingerprint,
    )

    ws = tmp_path / "workspace"
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    skill = SkillDefinition(
        id="sk1",
        name="demo-skill",
        description="d",
        content="---\nname: demo-skill\ndescription: d\n---\nbody",
        config={},
        status="active",
    )
    path1 = materialize_agent_skills(settings, "agent1", [skill], workspace_root=ws)
    fp = skills_fingerprint([skill])
    skill_file = ws / "skills" / fp / "agent1" / "demo-skill" / "SKILL.md"
    assert path1 == f"/skills/{fp}/agent1/"
    assert skill_file.is_file()
    mtime1 = skill_file.stat().st_mtime_ns

    path2 = materialize_agent_skills(settings, "agent1", [skill], workspace_root=ws)
    assert path2 == path1
    assert skill_file.stat().st_mtime_ns == mtime1


def test_cache_eviction_keeps_materialized_skills(tmp_path, monkeypatch):
    """缓存淘汰只丢构建锁，不删已物化的 Skills 目录。"""
    from deepagents_app import config
    from deepagents_app.config import Settings
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.ownership import user_scope_key
    from deepagents_app.services.runtime import agent_factory as af
    from deepagents_app.services.catalog.skills import (
        materialize_agent_skills,
        skills_fingerprint,
    )
    from deepagents_app.workspace import user_workspace_dir

    ws = tmp_path / "workspace"
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    skill = SkillDefinition(
        id="sk1",
        name="keep-skill",
        description="d",
        content="---\nname: keep-skill\ndescription: d\n---\nbody",
        config={},
        status="active",
    )
    user_ws = user_workspace_dir(settings, "alice")
    materialize_agent_skills(
        settings, "agent1", [skill], workspace_root=user_ws
    )
    fp = skills_fingerprint([skill])
    skill_file = (
        user_ws / "skills" / fp / "agent1" / "keep-skill" / "SKILL.md"
    )
    assert skill_file.is_file()

    key = af.cache_key("alice", "meth1", 1)
    af._cache[key] = "AGENT"
    af._build_lock_for(key)
    af._drop_build_lock(key)
    assert key not in af._build_locks
    assert skill_file.is_file()
    assert user_scope_key("alice") in str(user_ws)


def test_skills_gc_removes_stale_and_keeps_fresh(tmp_path, monkeypatch):
    """GC 删除过期 .complete，保留近期刷新的目录，并清临时目录。"""
    import os
    import time

    from deepagents_app import config
    from deepagents_app.config import Settings
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.services.catalog.skills import (
        _COMPLETE_MARKER,
        gc_materialized_skills,
        materialize_agent_skills,
        skills_fingerprint,
    )
    from deepagents_app.workspace import user_workspace_dir

    ws = tmp_path / "workspace"
    settings = Settings(
        workspace_dir=ws,
        skills_gc_max_age_days=1,
        skills_gc_tmp_max_age_hours=1,
    )
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    stale = SkillDefinition(
        id="sk-stale",
        name="stale-skill",
        description="d",
        content="---\nname: stale-skill\ndescription: d\n---\nold",
        config={},
        status="active",
    )
    fresh = SkillDefinition(
        id="sk-fresh",
        name="fresh-skill",
        description="d",
        content="---\nname: fresh-skill\ndescription: d\n---\nnew",
        config={},
        status="active",
    )
    user_ws = user_workspace_dir(settings, "bob")
    materialize_agent_skills(
        settings, "agent_stale", [stale], workspace_root=user_ws
    )
    materialize_agent_skills(
        settings, "agent_fresh", [fresh], workspace_root=user_ws
    )
    fp_stale = skills_fingerprint([stale])
    fp_fresh = skills_fingerprint([fresh])
    stale_dir = user_ws / "skills" / fp_stale / "agent_stale"
    fresh_dir = user_ws / "skills" / fp_fresh / "agent_fresh"
    assert (stale_dir / _COMPLETE_MARKER).is_file()
    assert (fresh_dir / _COMPLETE_MARKER).is_file()

    old = time.time() - 3 * 86400
    os.utime(stale_dir / _COMPLETE_MARKER, (old, old))

    tmp_dir = user_ws / "skills" / fp_stale / "agent_stale.tmp-deadbeef"
    tmp_dir.mkdir(parents=True)
    os.utime(tmp_dir, (old, old))

    stats = gc_materialized_skills(settings, now=time.time())
    assert stats["removed_agents"] >= 1
    assert stats["removed_tmp"] >= 1
    assert not stale_dir.exists()
    assert not tmp_dir.exists()
    assert (fresh_dir / _COMPLETE_MARKER).is_file()


def test_create_lonely_agent_does_not_flush_all_cache(client):
    """新建未被方法论引用的 Agent 不应清空全体编译缓存。"""
    import deepagents_app.services.runtime.agent_factory as af

    af._cache["someone_else:v1"] = "OTHER"
    af._cache["mine:v3"] = "MINE"
    created = client.post(
        "/api/agent",
        json={
            "name": "lonely-agent-cache",
            "system_prompt": "x",
            "config": {"role": "subagent"},
        },
    )
    assert created.status_code == 200
    assert af._cache.get("someone_else:v1") == "OTHER"
    assert af._cache.get("mine:v3") == "MINE"


def test_openai_compatible_extra_base_url_no_typeerror():
    """extra 含 base_url 时不应与显式参数撞车。"""
    from deepagents_app.config import Settings
    from deepagents_app.llm import build_chat_model

    settings = Settings(
        model_provider="openai_compatible",
        model_name="demo",
        openai_base_url="http://fallback",
    )
    model = build_chat_model(
        settings,
        provider="openai_compatible",
        model_name="x",
        base_url="http://a",
        api_key="k",
        extra={"base_url": "http://b", "api_key": "from-extra"},
    )
    # 显式参数优先；不应抛 TypeError
    assert getattr(model, "openai_api_base", None) in {
        "http://a",
        "http://a/",
    } or str(getattr(model, "openai_api_base", "")).startswith("http://a")


def test_model_api_key_encrypted_at_rest(client, tmp_path):
    """模型 api_key 落库应为 enc:v1: 密文，响应不回传明文。"""
    from sqlalchemy.orm import Session

    from deepagents_app.crypto import decrypt_secret
    from deepagents_app.db.models import ModelDefinition
    from deepagents_app.db.session import get_engine

    created = client.post(
        "/api/model",
        json={
            "name": "enc-model",
            "provider": "openai_compatible",
            "model_name": "demo",
            "api_key": "sk-plain-secret",
        },
    )
    assert created.status_code == 200
    assert created.json()["has_api_key"] is True
    assert "api_key" not in created.json()

    db = Session(bind=get_engine())
    try:
        row = db.get(ModelDefinition, created.json()["id"])
        assert row is not None
        assert row.api_key is not None
        assert row.api_key.startswith("enc:v1:")
        assert "sk-plain-secret" not in row.api_key
        assert decrypt_secret(row.api_key) == "sk-plain-secret"
    finally:
        db.close()


def test_user_workspace_and_skills_materialize_isolation(tmp_path, monkeypatch):
    """不同用户物化到各自 workspace/users/<scope>/skills 下。"""
    from deepagents_app import config
    from deepagents_app.config import Settings
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.ownership import user_scope_key
    from deepagents_app.services.catalog.skills import (
        materialize_agent_skills,
        skills_fingerprint,
    )
    from deepagents_app.workspace import user_workspace_dir

    ws = tmp_path / "workspace"
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    skill = SkillDefinition(
        id="sk1",
        owner_user_id="u1",
        name="demo-skill",
        description="d",
        content="---\nname: demo-skill\ndescription: d\n---\nbody",
        config={},
        status="active",
    )
    u1 = user_workspace_dir(settings, "user-a")
    u2 = user_workspace_dir(settings, "user-b")
    assert u1 != u2
    assert user_scope_key("user-a") in str(u1)

    path = materialize_agent_skills(
        settings,
        "agent1",
        [skill],
        workspace_root=u1,
    )
    fp = skills_fingerprint([skill])
    assert path == f"/skills/{fp}/agent1/"
    skill_file = u1 / "skills" / fp / "agent1" / "demo-skill" / "SKILL.md"
    assert skill_file.is_file()
    assert not (u2 / "skills" / fp).exists()


def test_general_purpose_subagent_spec_and_no_global_profile():
    """组装侧提供 general-purpose；factory 不再导出全局注册函数。"""
    import deepagents_app.factory as factory

    assert not hasattr(factory, "configure_general_purpose_profile")
    spec = factory.build_general_purpose_subagent(
        model="sentinel-model",
        specialist_names=["researcher", "coder"],
    )
    assert spec["name"] == "general-purpose"
    assert spec["model"] == "sentinel-model"
    assert "researcher" in spec["description"]
    assert "coder" in spec["description"]


def test_cache_key_includes_user_scope():
    """缓存键带用户 scope。"""
    from deepagents_app.ownership import user_scope_key
    from deepagents_app.services.runtime import agent_factory as af
    key = af.cache_key("alice", "meth1", 3)
    assert key == f"{user_scope_key('alice')}:meth1:v3"


def test_list_pagination_headers(client):
    """列表支持 limit/offset，并通过 X-Total-Count 返回总数。"""
    r = client.get("/api/methodology/list", params={"limit": 1, "offset": 0})
    assert r.status_code == 200
    assert "X-Total-Count" in r.headers
    assert int(r.headers["X-Total-Count"]) >= 1
    assert len(r.json()) == 1




def test_middleware_list_ok(client):
    """A2 回归：中间件列表路由必须正确 await。"""
    r = client.get("/api/middleware/list")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list)
    assert len(body) >= 1
    assert "X-Total-Count" in r.headers
    mid = body[0]["id"]
    detail = client.get(f"/api/middleware/{mid}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["id"] == mid


def test_chat_e2e_with_fake_model(client, demo_ids, monkeypatch):
    """A1 回归：真实 AsyncRedisSaver + 假 ChatModel，SSE 聊天路径可跑通。"""
    import json
    import uuid

    from langchain_core.language_models.fake_chat_models import FakeListChatModel

    from deepagents_app.services.runtime import agent_factory as af
    from deepagents_app.services.catalog import llm_models as models_svc

    class _ToolAwareFake(FakeListChatModel):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ARG002
            return self

    replies = ["你好，这是端到端冒烟回复。"] * 16
    fake = _ToolAwareFake(responses=replies)

    async def _fake_chat_model(db, settings, agent, *, owner_user_id):  # noqa: ANN001
        return fake

    monkeypatch.setattr(af, "_chat_model_for_agent", _fake_chat_model)
    monkeypatch.setattr(
        models_svc,
        "resolve_model_spec_for_agent",
        lambda *a, **k: {
            "provider": "openai",
            "model_name": "fake",
            "api_key": None,
            "base_url": None,
            "temperature": 0,
            "top_p": None,
            "max_tokens": None,
            "timeout": None,
            "extra": {},
        },
    )

    af.invalidate_agent_cache()
    mid = demo_ids["methodology"]
    # 每次唯一 thread，避免复用本机 Redis 里的旧 checkpoint
    tid = f"e2e-chat-{uuid.uuid4().hex}"
    created = client.post(
        "/api/conversation",
        json={"methodology_id": mid, "thread_id": tid},
    )
    assert created.status_code == 200, created.text
    assert created.json()["thread_id"] == tid

    with client.stream(
        "POST",
        "/api/chat/stream",
        json={"thread_id": tid, "message": "打个招呼"},
    ) as chat:
        assert chat.status_code == 200, chat.text
        text = "".join(chat.iter_text())

    done: dict | None = None
    event_name = None
    for line in text.splitlines():
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:") and event_name == "done":
            done = json.loads(line.removeprefix("data:").strip())
            break
    assert done is not None, text
    assert done["thread_id"] == tid
    assert done.get("interrupted") is False
    assert done.get("reply")
    assert "冒烟" in done["reply"] or len(done["reply"]) > 0

    msgs = client.get(f"/api/conversation/{tid}/messages")
    assert msgs.status_code == 200, msgs.text
    history = msgs.json()
    assert history.get("messages") or history


def test_agent_enable_publishes_same_name_methodology(client):
    """主 Agent 启用即发布同名方法论；启用后禁止改任何信息。"""
    sub = client.post(
        "/api/agent",
        json={
            "name": "enable-sub",
            "system_prompt": "sub",
            "config": {"role": "subagent"},
        },
    ).json()
    sup = client.post(
        "/api/agent",
        json={
            "name": "enable-main",
            "system_prompt": "main",
            "config": {
                "role": "supervisor",
                "subagent_ids": [sub["id"]],
            },
        },
    ).json()
    assert sup["enabled"] is False

    enabled_sub = client.post(f"/api/agent/{sub['id']}/enable")
    assert enabled_sub.status_code == 200, enabled_sub.text
    assert enabled_sub.json()["enabled"] is True
    locked_sub = client.patch(
        f"/api/agent/{sub['id']}",
        json={"system_prompt": "nope"},
    )
    assert locked_sub.status_code == 400
    assert "先停用" in locked_sub.json()["detail"]

    enabled = client.post(f"/api/agent/{sup['id']}/enable")
    assert enabled.status_code == 200, enabled.text
    body = enabled.json()
    assert body["enabled"] is True
    mid = body["methodology_id"]
    assert mid
    meth = client.get(f"/api/methodology/{mid}")
    assert meth.status_code == 200
    assert meth.json()["name"] == "enable-main"
    assert meth.json()["status"] == "published"
    names = {a["name"] for a in meth.json()["agents"]}
    assert names == {"enable-main", "enable-sub"}

    locked = client.patch(
        f"/api/agent/{sup['id']}",
        json={"system_prompt": "changed"},
    )
    assert locked.status_code == 400
    tools_locked = client.post(
        f"/api/agent/{sup['id']}/tools",
        json={"tool_ids": []},
    )
    assert tools_locked.status_code == 400

    disabled = client.post(f"/api/agent/{sup['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert client.get(f"/api/methodology/{mid}").json()["status"] == "draft"

    patched = client.patch(
        f"/api/agent/{sup['id']}",
        json={"system_prompt": "after-disable"},
    )
    assert patched.status_code == 200
    assert patched.json()["system_prompt"] == "after-disable"
    assert patched.json()["enabled"] is False

    renamed = client.patch(
        f"/api/agent/{sup['id']}",
        json={"name": "enable-main-renamed"},
    )
    assert renamed.status_code == 200
    assert client.get(f"/api/methodology/{mid}").json()["name"] == "enable-main-renamed"

    conv_draft_blocked = client.post("/api/conversation", json={"methodology_id": mid})
    assert conv_draft_blocked.status_code == 400

    reenabled = client.post(f"/api/agent/{sup['id']}/enable")
    assert reenabled.status_code == 200
    assert reenabled.json()["enabled"] is True
    meth2 = client.get(f"/api/methodology/{mid}").json()
    assert meth2["status"] == "published"
    assert meth2["name"] == "enable-main-renamed"
    assert meth2["version"] >= 2
    conv = client.post("/api/conversation", json={"methodology_id": mid})
    assert conv.status_code == 200
