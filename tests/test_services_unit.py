"""
services 层单测（不依赖 LLM / 不经 HTTP）。

运行::

    python -m pytest tests/test_services_unit.py -q
"""

from __future__ import annotations

import pytest

from tests.conftest import SVC_TEST_USER as TEST_USER


def test_validate_resource_id_rejects_path_traversal():
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.ownership import validate_resource_id

    with pytest.raises(BusinessError):
        validate_resource_id("../../../../PWNED")
    with pytest.raises(BusinessError):
        validate_resource_id("bad/id")
    validate_resource_id("ok_agent-1")


async def test_list_methodologies_pagination(db_session):
    from deepagents_app.services.catalog import methodology as methodology_svc
    rows, total, next_cursor = await methodology_svc.list_methodologies(
        db_session, owner_user_id=TEST_USER, limit=1, offset=0
    )
    assert total >= 1
    assert len(rows) == 1

    rows2, total2, _ = await methodology_svc.list_methodologies(
        db_session, owner_user_id=TEST_USER, limit=1, offset=1
    )
    assert total2 == total
    if total > 1:
        assert rows[0].id != rows2[0].id
        assert next_cursor is not None
        rows3, total3, _ = await methodology_svc.list_methodologies(
            db_session, owner_user_id=TEST_USER, limit=1, cursor=next_cursor
        )
        assert total3 == total
        assert rows3[0].id == rows2[0].id


async def test_pagination_cursor_roundtrip():
    from deepagents_app.db.pagination import decode_cursor, encode_cursor

    token = encode_cursor(sort="2026-01-01T00:00:00+00:00", id="abc")
    sort, item_id = decode_cursor(token)
    assert sort == "2026-01-01T00:00:00+00:00"
    assert item_id == "abc"


async def test_create_methodology_rejects_bad_id(db_session):
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.services.catalog import methodology as methodology_svc
    with pytest.raises(BusinessError):
        await methodology_svc.create_methodology(
            db_session,
            owner_user_id=TEST_USER,
            name="evil",
            methodology_id="../escape",
        )


async def test_agent_cache_lru_eviction(db_session, monkeypatch, tmp_path):
    from deepagents_app import config
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services.runtime import agent_factory as af
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


async def test_bind_agents_returns_detail(db_session):
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services.catalog import agents as agents_svc
    from deepagents_app.services.catalog import methodology as methodology_svc
    mid = demo_methodology_id_for_user(TEST_USER)
    agents, _, _ = await agents_svc.list_agents(db_session, owner_user_id=TEST_USER, limit=10)
    assert agents
    # 草稿方法论：绑定不升版
    draft = await methodology_svc.create_methodology(
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

    original = get_settings().enable_hitl
    overridden = settings_with(enable_hitl=not original)
    assert overridden.enable_hitl is (not original)
    assert get_settings().enable_hitl is original
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


async def test_mcp_tools_cache_hit(monkeypatch):
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
    a = await tools_reg.load_mcp_tools(row)
    b = await tools_reg.load_mcp_tools(row)
    assert calls["n"] == 1
    assert len(a) == 1 and len(b) == 1
    tools_reg.clear_mcp_tools_cache(tool_id=row.id)


def test_mcp_cache_invalidation_applies_across_pubsub_helper(monkeypatch):
    """他机收到 MCP 失效消息后应清本地缓存（跳过本 worker_id）。"""
    from deepagents_app.db.models import ToolDefinition
    from deepagents_app.registries import tools as tools_reg
    from deepagents_app.services.infra import cache_pubsub
    tools_reg.clear_mcp_tools_cache()
    tools_reg._mcp_tools_cache["tool_x"] = ("fp", [object()])  # noqa: SLF001
    cache_pubsub._apply_mcp_local({"all": False, "tool_id": "tool_x"})
    assert "tool_x" not in tools_reg._mcp_tools_cache  # noqa: SLF001

    published: list[dict] = []

    def fake_publish(*, tool_id=None, all_keys=False):  # noqa: ANN001
        published.append({"tool_id": tool_id, "all_keys": all_keys})

    monkeypatch.setattr(
        cache_pubsub, "publish_mcp_cache_invalidation", fake_publish
    )
    tools_reg._mcp_tools_cache["tool_y"] = ("fp", [object()])  # noqa: SLF001
    tools_reg.invalidate_mcp_tools_cache(tool_id="tool_y")
    assert "tool_y" not in tools_reg._mcp_tools_cache  # noqa: SLF001
    assert published == [{"tool_id": "tool_y", "all_keys": False}]


async def test_seed_qa_tools_exist(db_session):
    from deepagents_app.db.models import ToolDefinition

    from sqlalchemy import select

    rows = list(
        await db_session.scalars(
            select(ToolDefinition)
            .where(ToolDefinition.owner_user_id == TEST_USER)
            .where(
                ToolDefinition.name.in_(
                    ("search_knowledge", "list_knowledge_topics", "save_qa_note")
                )
            )
        )
    )
    assert len(rows) == 3
    # 种子 QA 工具默认不强制 HITL；框架原生 write_file/edit_file/execute 仍受 enable_hitl 控制
    assert all(not r.requires_hitl for r in rows)


async def test_interrupt_tool_names_from_payloads():
    from deepagents_app.registries.tools import interrupt_tool_names_from_payloads

    names = await interrupt_tool_names_from_payloads(
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
    from deepagents_app.services.runtime.agent_factory import _resolve_interrupt_on

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
    catalog_only = _resolve_interrupt_on(
        settings,
        supervisor_config={},
        catalog_interrupt_on={"run_shell_command": True},
    )
    assert catalog_only == {"run_shell_command": True}
    assert "write_file" not in catalog_only
    config.get_settings.cache_clear()


async def test_memory_versioned_in_snapshot_and_materialize(db_session, tmp_path):
    from deepagents_app.config import Settings
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services.versioning import memory as memory_mod
    from deepagents_app.services.versioning.content_blobs import get_content_blob
    from deepagents_app.services.versioning.memory import materialize_versioned_memory
    from deepagents_app.services.versioning.revisions import serialize_methodology
    from deepagents_app.workspace import user_workspace_dir

    pinned = "# pinned memory v-test\n"
    mid = demo_methodology_id_for_user(TEST_USER)

    original = memory_mod.read_project_memory
    memory_mod.read_project_memory = lambda _settings=None: pinned
    try:
        payload = await serialize_methodology(db_session, mid)
    finally:
        memory_mod.read_project_memory = original

    assert "content_hash" in payload["memory"]
    body = await get_content_blob(db_session, payload["memory"]["content_hash"])
    assert body == pinned

    settings = Settings(workspace_dir=tmp_path / "workspace")
    root = user_workspace_dir(settings, TEST_USER, ensure=True)
    virtual = materialize_versioned_memory(
        root,
        methodology_id=mid,
        version=int(payload["version"]),
        content=body,
    )
    assert virtual is not None
    assert virtual.startswith("/memory/")
    disk = root / virtual.lstrip("/")
    assert disk.is_file()
    assert disk.read_text(encoding="utf-8") == pinned

    v2 = materialize_versioned_memory(
        root,
        methodology_id=mid,
        version=int(payload["version"]) + 1,
        content="# memory v2\n",
    )
    assert (root / v2.lstrip("/")).read_text(encoding="utf-8") == "# memory v2\n"
    assert disk.read_text(encoding="utf-8") == pinned


def test_secrets_previous_keys_decrypt(monkeypatch):
    from cryptography.fernet import Fernet

    from deepagents_app import config
    from deepagents_app.crypto import (
        clear_fernet_cache,
        decrypt_secret,
        encrypt_secret,
    )

    old_key = Fernet.generate_key().decode()
    new_key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", old_key)
    monkeypatch.delenv("SECRETS_ENCRYPTION_PREVIOUS_KEYS", raising=False)
    config.get_settings.cache_clear()
    clear_fernet_cache()
    ciphertext = encrypt_secret("sk-rotate-me")

    monkeypatch.setenv("SECRETS_ENCRYPTION_KEY", new_key)
    monkeypatch.setenv("SECRETS_ENCRYPTION_PREVIOUS_KEYS", old_key)
    config.get_settings.cache_clear()
    clear_fernet_cache()
    assert decrypt_secret(ciphertext) == "sk-rotate-me"

    monkeypatch.delenv("SECRETS_ENCRYPTION_PREVIOUS_KEYS", raising=False)
    config.get_settings.cache_clear()
    clear_fernet_cache()
    with pytest.raises(ValueError, match="解密失败"):
        decrypt_secret(ciphertext)

    clear_fernet_cache()
    config.get_settings.cache_clear()


async def test_checkpointer_requires_redis():
    import deepagents_app.factory as factory
    from deepagents_app.config import Settings

    await factory.close_checkpointer()
    settings = Settings(redis_url="redis://127.0.0.1:1")
    with pytest.raises(RuntimeError, match="Redis checkpointer 不可用"):
        await factory.init_checkpointer(settings)
    with pytest.raises(RuntimeError, match="尚未初始化"):
        factory.build_checkpointer(settings)
    await factory.close_checkpointer()


async def test_publish_rejects_multiple_supervisors(db_session):
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.ownership import scoped_id
    from deepagents_app.services.catalog import agents as agents_svc
    from deepagents_app.services.catalog import methodology as methodology_svc
    a1 = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-a",
        system_prompt="a",
        config={"role": "supervisor"},
        bump_related=False,
    )
    a2 = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-b",
        system_prompt="b",
        config={"role": "supervisor"},
        bump_related=False,
    )
    meth = await methodology_svc.create_methodology(
        db_session,
        owner_user_id=TEST_USER,
        name="multi-sup",
        agent_ids=[a1.id, a2.id],
        methodology_id=scoped_id(TEST_USER, "meth_multi_sup"),
    )
    with pytest.raises(BusinessError, match="只能有一个 Supervisor"):
        await methodology_svc.publish_methodology(
            db_session, meth.id, owner_user_id=TEST_USER
        )


async def test_publish_ignores_disabled_extra_supervisor(db_session):
    """disabled Supervisor 不计入；与组装口径一致，允许发布。"""
    from deepagents_app.ownership import scoped_id
    from deepagents_app.services.catalog import agents as agents_svc
    from deepagents_app.services.catalog import methodology as methodology_svc

    active = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-on",
        system_prompt="on",
        config={"role": "supervisor", "enabled": True},
        bump_related=False,
    )
    disabled = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-off",
        system_prompt="off",
        config={"role": "supervisor", "enabled": False},
        bump_related=False,
    )
    meth = await methodology_svc.create_methodology(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-enabled-filter",
        agent_ids=[active.id, disabled.id],
        methodology_id=scoped_id(TEST_USER, "meth_sup_enabled"),
    )
    published = await methodology_svc.publish_methodology(
        db_session, meth.id, owner_user_id=TEST_USER
    )
    assert published.status == "published"


async def test_publish_rejects_only_disabled_supervisor(db_session):
    """唯一 Supervisor 被禁用时发布失败（与组装「缺少 Supervisor」一致）。"""
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.ownership import scoped_id
    from deepagents_app.services.catalog import agents as agents_svc
    from deepagents_app.services.catalog import methodology as methodology_svc

    disabled_sup = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sup-disabled",
        system_prompt="x",
        config={"role": "supervisor", "enabled": False},
        bump_related=False,
    )
    sub = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="sub-only",
        system_prompt="y",
        config={"role": "subagent", "enabled": True},
        bump_related=False,
    )
    meth = await methodology_svc.create_methodology(
        db_session,
        owner_user_id=TEST_USER,
        name="no-enabled-sup",
        agent_ids=[disabled_sup.id, sub.id],
        methodology_id=scoped_id(TEST_USER, "meth_no_en_sup"),
    )
    with pytest.raises(BusinessError, match="缺少启用中的 Supervisor"):
        await methodology_svc.publish_methodology(
            db_session, meth.id, owner_user_id=TEST_USER
        )


async def test_bind_foreign_tool_returns_forbidden(db_session):
    from deepagents_app.api.errors import ForbiddenError
    from deepagents_app.db.seed import ensure_user_bootstrap
    from deepagents_app.ownership import scoped_id
    from deepagents_app.services.catalog import agents as agents_svc
    from deepagents_app.services.catalog import tools as tools_svc
    other = "other-owner"
    await ensure_user_bootstrap(db_session, other)
    foreign_tool = await tools_svc.get_tool(
        db_session,
        scoped_id(other, "tool_search_knowledge"),
        owner_user_id=other,
    )
    assert foreign_tool is not None

    agent = await agents_svc.create_agent(
        db_session,
        owner_user_id=TEST_USER,
        name="own-agent",
        system_prompt="x",
        config={"role": "subagent"},
        bump_related=False,
    )
    with pytest.raises(ForbiddenError, match="不属于当前用户"):
        await agents_svc.bind_agent_tools(
            db_session,
            agent.id,
            [foreign_tool.id],
            owner_user_id=TEST_USER,
        )



async def test_content_blob_dedup_and_gc(db_session):
    from deepagents_app.services.versioning.content_blobs import (
        ensure_content_blob,
        gc_orphan_content_blobs,
        get_content_blob,
    )

    h1 = await ensure_content_blob(db_session, "hello-blob")
    h2 = await ensure_content_blob(db_session, "hello-blob")
    assert h1 == h2
    assert await get_content_blob(db_session, h1) == "hello-blob"
    # 无快照引用 → GC 可删（测试跳过宽限期）
    deleted = await gc_orphan_content_blobs(db_session, min_age_seconds=0)
    assert deleted >= 1
    assert await get_content_blob(db_session, h1) is None


async def test_hydrate_missing_blob_raises(db_session):
    from deepagents_app.api.errors import NotFoundError
    from deepagents_app.services.versioning.content_blobs import hydrate_snapshot_content

    with pytest.raises(NotFoundError, match="正文缺失"):
        await hydrate_snapshot_content(
            db_session,
            {
                "agents": [
                    {
                        "id": "a1",
                        "name": "a",
                        "system_prompt_hash": "deadbeef" * 8,
                    }
                ]
            },
        )


async def test_gc_second_pass_keeps_rereferenced_hash(db_session, monkeypatch):
    """删除前二次扫引用：第一次像孤儿、第二次又被引用时不得删。"""
    from deepagents_app.services.versioning import content_blobs as blobs_mod
    from deepagents_app.services.versioning.content_blobs import (
        ensure_content_blob,
        gc_orphan_content_blobs,
        get_content_blob,
    )

    digest = await ensure_content_blob(db_session, "keep-me-blob")
    calls = {"n": 0}

    async def _flaky_collect(_db):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            return set()
        return {digest}

    monkeypatch.setattr(blobs_mod, "collect_referenced_content_hashes", _flaky_collect)
    await gc_orphan_content_blobs(db_session, min_age_seconds=0)
    assert calls["n"] == 2
    assert await get_content_blob(db_session, digest) == "keep-me-blob"


def test_touch_materialized_skills_complete(tmp_path):
    from deepagents_app.services.catalog.skills import (
        materialized_skills_dir_from_virtual,
        touch_materialized_skills_complete,
    )

    root = tmp_path / "skills" / "abc123" / "agent1"
    root.mkdir(parents=True)
    marker = root / ".complete"
    marker.write_text("", encoding="utf-8")
    old_mtime = marker.stat().st_mtime - 100
    import os

    os.utime(marker, (old_mtime, old_mtime))
    virtual = "/skills/abc123/agent1/"
    resolved = materialized_skills_dir_from_virtual(tmp_path, virtual)
    assert resolved == root.resolve()
    assert touch_materialized_skills_complete([resolved]) == 1
    assert marker.stat().st_mtime > old_mtime


def test_serialize_interrupts_structured():
    from types import SimpleNamespace

    from deepagents_app.services.runtime.chat import serialize_interrupts

    interrupt = SimpleNamespace(
        id="irq-1",
        value={
            "action_requests": [
                {"name": "run_shell_command", "args": {"command": "ls"}, "description": "shell"}
            ]
        },
    )
    packed = serialize_interrupts([interrupt])
    assert packed is not None
    assert packed[0]["id"] == "irq-1"
    assert packed[0]["actions"][0]["name"] == "run_shell_command"
