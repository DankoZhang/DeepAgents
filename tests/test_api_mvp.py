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
            "api_key": "sk-test",
        },
    )
    assert created.status_code == 200
    body = created.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert body["top_p"] == 0.9
    assert "context_length" not in body
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


def test_draft_agent_edit_does_not_bump_version(client):
    """草稿方法论：被引用 Agent 变更只覆盖快照，不升版。"""
    client.post(
        "/api/methodology",
        json={"name": "草稿累积", "id": "draft_accum"},
    )
    agent = client.post(
        "/api/agent",
        json={
            "name": "da-supervisor",
            "system_prompt": "v1",
            "config": {"role": "supervisor"},
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

    # 发布后同样变更应升版
    published = client.post("/api/methodology/draft_accum/publish").json()
    assert published["status"] == "published"
    assert published["version"] == v0
    client.patch(
        f"/api/agent/{agent['id']}",
        json={"system_prompt": "v3-published"},
    )
    bumped = client.get("/api/methodology/draft_accum").json()
    assert bumped["version"] > v0


def test_agent_cache_lru_evicts_and_drops_build_lock(tmp_path, monkeypatch):
    """缓存超额时按 LRU 淘汰，并释放对应构建锁。"""
    from deepagents_app import config
    from deepagents_app.services import agent_factory as af

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
        af._cleanup_evicted_key(key)
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

    client.post("/api/methodology", json={"name": "裁剪", "id": "prune_meth"})
    agent = client.post(
        "/api/agent",
        json={
            "name": "prune-sup",
            "system_prompt": "s0",
            "config": {"role": "supervisor"},
        },
    ).json()
    client.post(
        "/api/methodology/prune_meth/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    client.post("/api/methodology/prune_meth/publish")
    v_pub = client.get("/api/methodology/prune_meth").json()["version"]
    conv = client.post(
        "/api/conversation",
        json={"methodology_id": "prune_meth"},
    ).json()
    assert conv["methodology_version"] == v_pub

    for i in range(4):
        client.patch(
            f"/api/agent/{agent['id']}",
            json={"system_prompt": f"s{i+1}"},
        )

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
            "config": {"role": "supervisor"},
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
    """即便历史脏数据进入物化，也不得 rmtree workspace 之外。"""
    from deepagents_app import config
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.config import Settings
    from deepagents_app.services.skills import (
        clear_materialized_skills,
        materialize_agent_skills,
    )

    ws = tmp_path / "workspace"
    ws.mkdir()
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    with pytest.raises(BusinessError):
        clear_materialized_skills(settings, scope="../../../../outside")
    with pytest.raises(BusinessError):
        materialize_agent_skills(
            settings, "../../PWNAGENT", [], scope="safe_mid/v1"
        )
    with pytest.raises(BusinessError):
        materialize_agent_skills(
            settings, "agent_ok", [], scope="../../evil/v1"
        )
    # 合法路径应只落在 workspace/skills 下
    from deepagents_app.services.skills import _safe_materialize_root

    root = _safe_materialize_root(
        settings, scope="safe_mid/v1", agent_id="agent_ok"
    )
    assert root.is_relative_to((ws / "skills").resolve())
    assert not (tmp_path / "outside").exists()
    assert not (tmp_path / "evil").exists()
    assert not (ws.parent / "PWNAGENT").exists()


def test_create_lonely_agent_does_not_flush_all_cache(client):
    """新建未被方法论引用的 Agent 不应清空全体编译缓存。"""
    import deepagents_app.services.agent_factory as af

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
    from deepagents_app.crypto import decrypt_secret
    from deepagents_app.db.models import ModelDefinition
    from deepagents_app.db.session import get_session_factory

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

    factory = get_session_factory()
    db = factory()
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
    from deepagents_app.services.skills import materialize_agent_skills
    from deepagents_app.workspace import (
        interprocess_lock,
        skills_materialize_lock,
        user_workspace_dir,
    )

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

    scope = "meth_a/v1"
    lock_path = skills_materialize_lock(u1, scope)
    with interprocess_lock(lock_path):
        path = materialize_agent_skills(
            settings,
            "agent1",
            [skill],
            scope=scope,
            workspace_root=u1,
        )
    assert path == "/skills/meth_a/v1/agent1/"
    skill_file = u1 / "skills" / "meth_a" / "v1" / "agent1" / "demo-skill" / "SKILL.md"
    assert skill_file.is_file()
    assert not (u2 / "skills" / "meth_a").exists()
    assert lock_path.is_file()


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
    """缓存键带用户 scope，清理路径指向 users/<uhash>。"""
    from deepagents_app.ownership import user_scope_key
    from deepagents_app.services import agent_factory as af

    key = af.cache_key("alice", "meth1", 3)
    assert key == f"{user_scope_key('alice')}:meth1:v3"
    parsed = af._parse_cache_key(key)
    assert parsed == (user_scope_key("alice"), "meth1", 3)


def test_list_pagination_headers(client):
    """列表支持 limit/offset，并通过 X-Total-Count 返回总数。"""
    r = client.get("/api/methodology/list", params={"limit": 1, "offset": 0})
    assert r.status_code == 200
    assert "X-Total-Count" in r.headers
    assert int(r.headers["X-Total-Count"]) >= 1
    assert len(r.json()) == 1


