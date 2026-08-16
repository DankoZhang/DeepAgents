#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   test_concurrency_sse.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   test_concurrency_sse.py

并发与 SSE 心跳回归（不依赖 LLM）。
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


def _stub_prepared(*, settings: Any | None = None) -> Any:
    resolved = settings or type(
        "S",
        (),
        {"workspace_dir": None, "chat_stream_max_concurrent": 0},
    )()

    class _Prepared:
        user_id = "u"
        thread_id = "t"
        methodology_id = "m"
        methodology_version = 1
        agent = object()
        config: dict[str, Any] = {}

    prepared = _Prepared()
    prepared.settings = resolved
    return prepared


def _stub_workspace(monkeypatch, chat_svc) -> None:
    @contextmanager
    def _ws(_root):
        yield Path("/tmp")

    monkeypatch.setattr(chat_svc, "workspace_context", _ws)
    monkeypatch.setattr(
        chat_svc, "user_workspace_dir", lambda *a, **k: Path("/tmp")
    )


def _sse_event_names(frames: list[str]) -> list[str]:
    events: list[str] = []
    for frame in frames:
        if frame.startswith("event: "):
            events.append(frame.split("\n", 1)[0].removeprefix("event: ").strip())
    return events


@pytest.mark.asyncio
async def test_build_lock_allows_concurrent_await():
    """同一 key 的两个并发请求不应挂死事件循环（asyncio.Lock）。"""
    from deepagents_app.services.runtime import agent_factory as af
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
    from deepagents_app.services.runtime import agent_factory as af
    af.invalidate_agent_cache()
    key = af.cache_key("failed-build-user", "failed-meth", 1)

    async def fail_load(*_args, **_kwargs):
        raise RuntimeError("模拟组装失败")

    monkeypatch.setattr(af, "load_methodology_for_assembly", fail_load)
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
    from deepagents_app.services.runtime import chat as chat_svc
    monkeypatch.setattr(chat_svc, "_SSE_PING_INTERVAL_SECONDS", 0.05)

    async def fake_stream(agent, payload, config):
        yield "token", {"text": "开头"}
        await asyncio.sleep(0.12)  # > ping interval
        yield "token", {"text": "结尾"}

    async def fake_final(agent, config):
        return {"messages": []}

    monkeypatch.setattr(chat_svc, "_aiter_stream_events", fake_stream)
    monkeypatch.setattr(chat_svc, "_afinal_state_result", fake_final)
    _stub_workspace(monkeypatch, chat_svc)

    frames: list[str] = []
    async for frame in chat_svc._aiter_sse(
        _stub_prepared(), {}, meta={"thread_id": "t"}, log_label="test"
    ):
        frames.append(frame)

    events = _sse_event_names(frames)
    assert "ping" in events
    assert events.count("token") == 2
    assert "done" in events


@pytest.mark.asyncio
async def test_sse_busy_stream_renews_lease_without_idle_ping(monkeypatch):
    """持续出事件（无空闲 ping）时，也应按间隔续租，避免忙流突破限流。"""
    from deepagents_app.services.runtime import chat as chat_svc

    monkeypatch.setattr(chat_svc, "_SSE_PING_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(chat_svc, "_SSE_LEASE_RENEW_INTERVAL_SECONDS", 0.05)

    async def fake_stream(agent, payload, config):
        # 间隔小于 ping timeout，全程无空闲 ping；总时长超过续期间隔
        for i in range(6):
            await asyncio.sleep(0.02)
            yield "token", {"text": str(i)}

    async def fake_final(agent, config):
        return {"messages": []}

    class _Slot:
        def __init__(self) -> None:
            self.renew_count = 0

        async def renew(self) -> None:
            self.renew_count += 1

    monkeypatch.setattr(chat_svc, "_aiter_stream_events", fake_stream)
    monkeypatch.setattr(chat_svc, "_afinal_state_result", fake_final)
    _stub_workspace(monkeypatch, chat_svc)

    slot = _Slot()
    frames: list[str] = []
    async for frame in chat_svc._aiter_sse(
        prepared=_stub_prepared(),
        payload={},
        meta={"thread_id": "t"},
        log_label="test",
        stream_slot=slot,
    ):
        frames.append(frame)

    events = _sse_event_names(frames)
    assert "ping" not in events
    assert events.count("token") == 6
    assert slot.renew_count >= 1


@pytest.mark.asyncio
async def test_sse_cancellation_closes_underlying_event_iterator(monkeypatch):
    """客户端断连取消外层 SSE 后，应关闭持有 LLM 流的内层异步生成器。"""
    from contextlib import suppress

    from deepagents_app.services.runtime import chat as chat_svc
    closed = asyncio.Event()
    entered = asyncio.Event()

    async def fake_stream(agent, payload, config):
        try:
            entered.set()
            await asyncio.Event().wait()
            yield "token", {"text": "不会到这里"}
        finally:
            closed.set()

    monkeypatch.setattr(chat_svc, "_aiter_stream_events", fake_stream)
    _stub_workspace(monkeypatch, chat_svc)

    stream = chat_svc._aiter_sse(
        _stub_prepared(settings=object()),
        {},
        meta={"thread_id": "t"},
        log_label="test",
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
    from deepagents_app.services.catalog.methodology import (
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


@pytest.mark.asyncio
async def test_redis_stream_lease_acquire_release_and_limit(monkeypatch):
    """Redis ZSET 租约：占满后 429；release 后可再申请；崩溃槽位过期可回收。"""
    from deepagents_app.api.errors import CapacityError
    from deepagents_app.config import Settings
    from deepagents_app.services.infra.redis_conn import (
        close_shared_redis,
        get_shared_redis,
    )
    from deepagents_app.services.runtime import stream_limiter as sl

    monkeypatch.setattr(sl, "_REDIS_LEASE_TTL_SECONDS", 2.0)
    await close_shared_redis()

    settings = Settings(
        redis_url="redis://localhost:6379",
        chat_stream_max_concurrent=2,
        chat_stream_acquire_timeout_seconds=0,
        chat_stream_limiter="redis",
        api_workers=1,
    )
    # 清掉测试 key，避免脏数据
    client = await get_shared_redis()
    await client.delete(sl._REDIS_LEASE_KEY)

    s1 = await sl.acquire_stream_slot(settings)
    s2 = await sl.acquire_stream_slot(settings)
    assert s1 is not None and s2 is not None
    with pytest.raises(CapacityError, match="繁忙"):
        await sl.acquire_stream_slot(settings)

    await s1.release()
    s3 = await sl.acquire_stream_slot(settings)
    assert s3 is not None

    # 模拟崩溃：不 release s2，等 TTL 过期后可再占
    await asyncio.sleep(2.2)
    s4 = await sl.acquire_stream_slot(settings)
    assert s4 is not None

    await s3.release()
    await s4.release()
    await close_shared_redis()


@pytest.mark.asyncio
async def test_redis_stream_lease_renew_extends_life(monkeypatch):
    """ping 续租后，槽位不应在原 TTL 到期时被当成过期清掉。"""
    from deepagents_app.config import Settings
    from deepagents_app.services.infra.redis_conn import (
        close_shared_redis,
        get_shared_redis,
    )
    from deepagents_app.services.runtime import stream_limiter as sl

    monkeypatch.setattr(sl, "_REDIS_LEASE_TTL_SECONDS", 1.0)
    await close_shared_redis()

    settings = Settings(
        redis_url="redis://localhost:6379",
        chat_stream_max_concurrent=1,
        chat_stream_acquire_timeout_seconds=0,
        chat_stream_limiter="redis",
        api_workers=1,
    )
    client = await get_shared_redis()
    await client.delete(sl._REDIS_LEASE_KEY)

    slot = await sl.acquire_stream_slot(settings)
    assert slot is not None
    await asyncio.sleep(0.6)
    await slot.renew()
    await asyncio.sleep(0.6)
    # 若未续租，此时已超过 1s；续租后仍应占满限额
    from deepagents_app.api.errors import CapacityError

    with pytest.raises(CapacityError):
        await sl.acquire_stream_slot(settings)

    await slot.release()
    again = await sl.acquire_stream_slot(settings)
    assert again is not None
    await again.release()
    await close_shared_redis()
