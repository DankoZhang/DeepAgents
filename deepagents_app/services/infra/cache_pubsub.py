"""
跨 worker 缓存失效广播
======================

本进程清缓存后，经 Redis pub/sub 通知其他 worker 执行本地失效：

- Agent 编译 LRU（draft 同 version 覆盖后他机勿命中旧编译体）
- MCP 工具列表（改连接配置后他机勿复用旧展开结果）
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

AGENT_CHANNEL = "deepagents:agent_cache_invalidate"
MCP_CHANNEL = "deepagents:mcp_cache_invalidate"
# 旧名兼容（仅文档/外部引用）；逻辑统一用 AGENT_CHANNEL
CHANNEL = AGENT_CHANNEL

_WORKER_ID = uuid.uuid4().hex

_listener_task: asyncio.Task[None] | None = None
_listener_stop: asyncio.Event | None = None
_publish_client: Any | None = None
_publish_lock = asyncio.Lock()
_publish_tasks: set[asyncio.Task[None]] = set()


def _apply_agent_local(payload: dict[str, Any]) -> None:
    from deepagents_app.services.runtime.agent_factory import invalidate_agent_cache_local

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


def _apply_mcp_local(payload: dict[str, Any]) -> None:
    from deepagents_app.registries.tools import clear_mcp_tools_cache

    if payload.get("all"):
        clear_mcp_tools_cache()
        return
    tool_id = payload.get("tool_id")
    clear_mcp_tools_cache(tool_id=tool_id if isinstance(tool_id, str) else None)


def _apply_message(channel: str, payload: dict[str, Any]) -> None:
    if channel == MCP_CHANNEL:
        _apply_mcp_local(payload)
        return
    # 默认按 Agent 频道处理（含未知/旧消息）
    _apply_agent_local(payload)


async def _apublish(channel: str, payload: dict[str, Any]) -> None:
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
        await client.publish(channel, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("广播缓存失效失败 channel=%s: %s", channel, exc)


def _track_publish_task(task: asyncio.Task[None]) -> None:
    _publish_tasks.add(task)
    task.add_done_callback(_publish_tasks.discard)


def _publish(channel: str, payload: dict[str, Any], *, task_name: str) -> None:
    """
    尽力向 Redis 广播；不阻塞调用方。

    有运行中的 loop 时 ``create_task`` 异步发布（登记 in-flight，shutdown 会 join）；
    否则同步尽力发布（测试/CLI）。
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is not None and loop.is_running():
        task = loop.create_task(_apublish(channel, payload), name=task_name)
        _track_publish_task(task)
        return

    try:
        import redis

        from deepagents_app.config import get_settings

        client = redis.Redis.from_url(
            get_settings().redis_url,
            socket_connect_timeout=1.5,
            socket_timeout=1.5,
        )
        try:
            client.publish(channel, json.dumps(payload, ensure_ascii=False))
        finally:
            client.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("广播缓存失效失败 channel=%s: %s", channel, exc)


def publish_cache_invalidation(
    *,
    methodology_id: str | None = None,
    version: int | None = None,
    owner_user_id: str | None = None,
    all_keys: bool = False,
) -> None:
    """广播 Agent 编译缓存失效。"""
    payload = {
        "worker_id": _WORKER_ID,
        "all": bool(all_keys or methodology_id is None),
        "methodology_id": methodology_id,
        "version": version,
        "owner_user_id": owner_user_id,
    }
    _publish(AGENT_CHANNEL, payload, task_name="agent-cache-publish")


def publish_mcp_cache_invalidation(
    *,
    tool_id: str | None = None,
    all_keys: bool = False,
) -> None:
    """广播 MCP 工具列表缓存失效。"""
    payload = {
        "worker_id": _WORKER_ID,
        "all": bool(all_keys or tool_id is None),
        "tool_id": tool_id,
    }
    _publish(MCP_CHANNEL, payload, task_name="mcp-cache-publish")


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


def _decode_channel(raw: Any) -> str:
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return str(raw) if raw is not None else ""


async def start_cache_invalidation_listener() -> None:
    """在 lifespan 内启动后台订阅任务（Agent + MCP）。"""
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
                await pubsub.subscribe(AGENT_CHANNEL, MCP_CHANNEL)
                logger.info(
                    "已订阅缓存失效频道 %s, %s", AGENT_CHANNEL, MCP_CHANNEL
                )
                while not stop.is_set():
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1.0
                    )
                    if message is None:
                        await asyncio.sleep(0.05)
                        continue
                    channel = _decode_channel(message.get("channel"))
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
                    _apply_message(channel, payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("缓存失效订阅异常，将重试: %s", exc)
                await asyncio.sleep(2.0)
            finally:
                if pubsub is not None:
                    try:
                        await pubsub.unsubscribe(AGENT_CHANNEL, MCP_CHANNEL)
                        await pubsub.aclose()
                    except Exception:  # noqa: BLE001
                        pass
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass

    _listener_task = asyncio.create_task(_run(), name="cache-invalidate")


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
