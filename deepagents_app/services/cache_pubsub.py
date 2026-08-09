"""
跨 worker Agent 缓存失效广播
============================

本进程 ``invalidate_agent_cache`` 后，通过 Redis pub/sub 通知其他 worker
执行本地失效，避免 draft 同 version 覆盖后他机仍命中旧编译体。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

CHANNEL = "deepagents:agent_cache_invalidate"
_WORKER_ID = uuid.uuid4().hex

_listener_task: asyncio.Task[None] | None = None
_listener_stop: asyncio.Event | None = None
_publish_client: Any | None = None
_publish_lock = asyncio.Lock()
_publish_tasks: set[asyncio.Task[None]] = set()


def _apply_local(payload: dict[str, Any]) -> None:
    from deepagents_app.services.agent_factory import invalidate_agent_cache_local

    if payload.get("all"):
        invalidate_agent_cache_local()
        return
    mid = payload.get("methodology_id")
    version = payload.get("version")
    owner = payload.get("owner_user_id")
    invalidate_agent_cache_local(
        mid if isinstance(mid, str) else None,
        int(version) if isinstance(version, int) else None,
        owner_user_id=owner if isinstance(owner, str) else None,
    )


async def _apublish(payload: dict[str, Any]) -> None:
    """异步发布；失败只打日志。"""
    global _publish_client
    try:
        import redis.asyncio as aioredis

        from deepagents_app.config import get_settings

        async with _publish_lock:
            if _publish_client is None:
                _publish_client = aioredis.from_url(
                    get_settings().redis_url,
                    socket_connect_timeout=1.5,
                    socket_timeout=1.5,
                )
            client = _publish_client
        await client.publish(CHANNEL, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("广播 Agent 缓存失效失败: %s", exc)


def _track_publish_task(task: asyncio.Task[None]) -> None:
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)


def publish_cache_invalidation(
    *,
    methodology_id: str | None = None,
    version: int | None = None,
    owner_user_id: str | None = None,
    all_keys: bool = False,
) -> None:
    """
    尽力向 Redis 广播；不阻塞调用方。

    有运行中的 loop 时 ``create_task`` 异步发布（登记 in-flight，shutdown 会 join）；
    否则同步尽力发布（测试/CLI）。
    """
    payload = {
        "worker_id": _WORKER_ID,
        "all": bool(all_keys or methodology_id is None),
        "methodology_id": methodology_id,
        "version": version,
        "owner_user_id": owner_user_id,
    }
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        task = loop.create_task(_apublish(payload), name="agent-cache-publish")
        _track_publish_task(task)
        return

    # 无事件循环（极少）：同步客户端兜底
    try:
        import redis

        from deepagents_app.config import get_settings

        client = redis.Redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        try:
            client.publish(CHANNEL, json.dumps(payload, ensure_ascii=False))
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("广播 Agent 缓存失效失败: %s", exc)


async def close_cache_invalidation_publisher() -> None:
    global _publish_client
    pending = list(_publish_tasks)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    async with _publish_lock:
        client = _publish_client
        _publish_client = None
    if client is not None:
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001
            logger.debug("关闭 cache invalidate publisher 失败", exc_info=True)


async def start_cache_invalidation_listener() -> None:
    """在 lifespan 内启动后台订阅任务。"""
    global _listener_task, _listener_stop
    if _listener_task is not None and not _listener_task.done():
        return
    _listener_stop = asyncio.Event()
    stop = _listener_stop

    async def _run() -> None:
        import redis.asyncio as aioredis

        from deepagents_app.config import get_settings

        url = get_settings().redis_url
        while not stop.is_set():
            client = None
            pubsub = None
            try:
                client = aioredis.from_url(url, socket_connect_timeout=1.5)
                pubsub = client.pubsub()
                await pubsub.subscribe(CHANNEL)
                logger.info("已订阅 Agent 缓存失效频道 %s", CHANNEL)
                while not stop.is_set():
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message is None:
                        await asyncio.sleep(0.05)
                        continue
                    raw = message.get("data")
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    if not isinstance(raw, str):
                        continue
                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    if payload.get("worker_id") == _WORKER_ID:
                        continue
                    _apply_local(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("缓存失效订阅异常，将重试: %s", exc)
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(CHANNEL)
                        await pubsub.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    _listener_task = asyncio.create_task(_run(), name="agent-cache-invalidate")


async def stop_cache_invalidation_listener() -> None:
    global _listener_task, _listener_stop
    if _listener_stop is not None:
        _listener_stop.set()
    task = _listener_task
    _listener_task = None
    if task is not None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.debug("停止 cache invalidate listener 失败", exc_info=True)
    await close_cache_invalidation_publisher()
