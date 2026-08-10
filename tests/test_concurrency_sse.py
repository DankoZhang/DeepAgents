"""并发与 SSE 心跳回归（不依赖 LLM）。"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_build_lock_allows_concurrent_await():
    """同一 key 的两个并发请求不应挂死事件循环（asyncio.Lock）。"""
    from deepagents_app.services import agent_factory as af

    key = af.cache_key("concurrent-user", "meth", 1)
    order: list[str] = []

    async def build(tag: str) -> str:
        async with af._build_lock_for(key):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-exit")
        return tag

    results = await asyncio.wait_for(
        asyncio.gather(build("a"), build("b")),
        timeout=2.0,
    )
    assert set(results) == {"a", "b"}
    assert order[0].endswith("-enter")
    assert order[-1].endswith("-exit")
    assert len(order) == 4


@pytest.mark.asyncio
async def test_failed_agent_build_drops_unowned_build_lock(monkeypatch):
    """编译失败后不应把永远不会进缓存的锁留在进程内字典。"""
    from deepagents_app.services import agent_factory as af

    af.invalidate_agent_cache()
    key = af.cache_key("failed-build-user", "failed-meth", 1)

    async def fail_load(*_args, **_kwargs):
        raise RuntimeError("模拟组装失败")

    monkeypatch.setattr(af, "get_methodology_config", fail_load)
    with pytest.raises(RuntimeError, match="模拟组装失败"):
        await af.build_agent_from_methodology(
            object(),
            "failed-meth",
            owner_user_id="failed-build-user",
            version=1,
            settings=object(),
        )

    assert key not in af._build_locks


@pytest.mark.asyncio
async def test_bootstrap_lock_allows_concurrent_await():
    """同一 user 的两个并发 bootstrap 锁不应挂死事件循环。"""
    from deepagents_app.db import seed

    order: list[str] = []

    async def boot(tag: str) -> str:
        async with seed._user_bootstrap_lock("alice"):
            order.append(f"{tag}-enter")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-exit")
        return tag

    results = await asyncio.wait_for(
        asyncio.gather(boot("a"), boot("b")),
        timeout=2.0,
    )
    assert set(results) == {"a", "b"}
    assert len(order) == 4


@pytest.mark.asyncio
async def test_sse_ping_does_not_kill_stream(monkeypatch):
    """心跳超时后仍应继续收到后续 token。"""
    from deepagents_app.services import chat as chat_svc

    monkeypatch.setattr(chat_svc, "_SSE_PING_INTERVAL_SECONDS", 0.05)

    async def fake_stream(agent, payload, config):
        yield "token", {"text": "开头"}
        await asyncio.sleep(0.12)  # > ping interval
        yield "token", {"text": "结尾"}

    async def fake_final(agent, config):
        return {"messages": []}

    monkeypatch.setattr(chat_svc, "_aiter_stream_events", fake_stream)
    monkeypatch.setattr(chat_svc, "_afinal_state_result", fake_final)

    class _Prepared:
        user_id = "u"
        thread_id = "t"
        methodology_id = "m"
        methodology_version = 1
        agent = object()
        settings = type(
            "S",
            (),
            {"workspace_dir": None, "chat_stream_max_concurrent": 0},
        )()
        config = {}

    # workspace_context 需要真实 Path；改 stub 掉 workspace helpers
    from pathlib import Path
    from contextlib import contextmanager

    @contextmanager
    def _ws(_root):
        yield Path("/tmp")

    monkeypatch.setattr(chat_svc, "workspace_context", _ws)
    monkeypatch.setattr(
        chat_svc, "user_workspace_dir", lambda *a, **k: Path("/tmp")
    )

    prepared = _Prepared()
    events: list[str] = []
    async for frame in chat_svc._aiter_sse(
        prepared, {}, meta={"thread_id": "t"}, log_label="test"
    ):
        if frame.startswith("event: "):
            events.append(frame.split("\n", 1)[0].removeprefix("event: ").strip())

    assert "ping" in events
    assert events.count("token") == 2
    assert "done" in events


@pytest.mark.asyncio
async def test_sse_cancellation_closes_underlying_event_iterator(monkeypatch):
    """客户端断连取消外层 SSE 后，应关闭持有 LLM 流的内层异步生成器。"""
    from contextlib import contextmanager, suppress
    from pathlib import Path

    from deepagents_app.services import chat as chat_svc

    closed = asyncio.Event()
    entered = asyncio.Event()

    async def fake_stream(agent, payload, config):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield "token", {"text": "不会到这里"}
        finally:
            closed.set()

    @contextmanager
    def _ws(_root):
        yield Path("/tmp")

    monkeypatch.setattr(chat_svc, "_aiter_stream_events", fake_stream)
    monkeypatch.setattr(chat_svc, "workspace_context", _ws)
    monkeypatch.setattr(
        chat_svc, "user_workspace_dir", lambda *args, **kwargs: Path("/tmp")
    )

    class _Prepared:
        user_id = "u"
        thread_id = "t"
        methodology_id = "m"
        methodology_version = 1
        agent = object()
        settings = object()
        config = {}

    stream = chat_svc._aiter_sse(
        _Prepared(), {}, meta={"thread_id": "t"}, log_label="test"
    )
    await anext(stream)  # meta；内层流尚未开始
    pending = asyncio.create_task(anext(stream))
    await asyncio.wait_for(entered.wait(), timeout=1)
    pending.cancel()
    with suppress(asyncio.CancelledError):
        await pending

    assert closed.is_set()


@pytest.mark.asyncio
async def test_draft_bump_does_not_write_snapshot(db_session):
    """draft 配置变更不写 revision、不跑 GC。"""
    from sqlalchemy import func, select

    from deepagents_app.db.models import MethodologyRevision
    from deepagents_app.ownership import scoped_id
    from deepagents_app.services.methodology import (
        bind_methodology_agents,
        create_methodology,
    )
    from tests.conftest import SVC_TEST_USER

    mid = "draft_no_snap"
    supervisor = scoped_id(SVC_TEST_USER, "agent_demo_supervisor")
    qa = scoped_id(SVC_TEST_USER, "agent_demo_qa_expert")

    await create_methodology(
        db_session,
        owner_user_id=SVC_TEST_USER,
        name="draft-no-snap",
        methodology_id=mid,
        agent_ids=[supervisor],
    )
    await db_session.commit()

    count_before = await db_session.scalar(
        select(func.count())
        .select_from(MethodologyRevision)
        .where(MethodologyRevision.methodology_id == mid)
    )
    assert count_before == 0

    await bind_methodology_agents(
        db_session, mid, [supervisor, qa], owner_user_id=SVC_TEST_USER
    )
    await db_session.commit()

    count_after = await db_session.scalar(
        select(func.count())
        .select_from(MethodologyRevision)
        .where(MethodologyRevision.methodology_id == mid)
    )
    assert count_after == 0
