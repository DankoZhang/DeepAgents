"""
Deep Agent 共享组装工具
======================

供 ``agent_factory`` 复用的底层能力：
checkpointer / HITL / permissions / general-purpose 子 Agent 规格。

方法论驱动的完整 Agent 组装见 ``deepagents_app.services.runtime.agent_factory``。
按方法论在组装时显式注入 ``general-purpose`` 子 Agent。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from deepagents import FilesystemPermission

from deepagents_app.config import Settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_checkpointer_key: str | None = None
# 必须用 asyncio.Lock：临界区内有 await，不可用 threading.Lock
_checkpointer_lock = asyncio.Lock()

__all__ = [
    "build_permissions",
    "build_interrupt_on",
    "build_checkpointer",
    "init_checkpointer",
    "close_checkpointer",
    "build_general_purpose_subagent",
]


def build_permissions() -> list[FilesystemPermission]:
    """
    声明式路径权限（first-match-wins）。

    示例策略：
    - 允许读写整个 workspace（backend root 映射为 /）
    - 拒绝直接改写审计日志（防止 Agent 篡改证据）
    """
    return [
        FilesystemPermission(
            paths=["/audit/**"],
            operations=["write"],
            mode="deny",
        ),
        FilesystemPermission(
            paths=["/**"],
            operations=["read", "write"],
            mode="allow",
        ),
    ]


# deepagents 框架自带、不在 ToolDefinition 目录中的危险工具
SYSTEM_HITL_TOOLS: tuple[str, ...] = ("write_file", "edit_file", "execute")


def build_interrupt_on(settings: Settings) -> dict[str, bool] | None:
    """
    系统默认 HITL 名单（仅框架原生工具）。

    目录工具（builtin / MCP）的审批由 ``ToolDefinition.requires_hitl`` 在组装时合并。
    关闭 ``enable_hitl`` 时返回 None（目录工具仍可单独合并，见 agent_factory）。
    """
    if not settings.enable_hitl:
        return None
    return {name: True for name in SYSTEM_HITL_TOOLS}


async def init_checkpointer(settings: Settings) -> Any:
    """
    在当前事件循环内建立（或复用）``AsyncRedisSaver`` 单例。

    必须在 FastAPI lifespan 的同一事件循环中调用；AsyncRedisSaver
    的连接绑定 loop，不可跨 loop 复用。
    """
    global _checkpointer, _checkpointer_key

    key = settings.redis_url
    old = None
    async with _checkpointer_lock:
        if _checkpointer is not None and _checkpointer_key == key:
            return _checkpointer

        try:
            from langgraph.checkpoint.redis.aio import AsyncRedisSaver

            saver = AsyncRedisSaver(redis_url=settings.redis_url)
            await saver.__aenter__()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Redis checkpointer 不可用（{settings.redis_url}）：{exc}"
            ) from exc

        old = _checkpointer
        _checkpointer = saver
        _checkpointer_key = key

    # 关闭旧实例放在锁外，避免持锁 await 拖长临界区
    if old is not None:
        try:
            await old.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.debug("关闭旧 AsyncRedisSaver 失败", exc_info=True)

    logger.info("Checkpointer: AsyncRedisSaver -> %s", settings.redis_url)
    return saver


def build_checkpointer(settings: Settings | None = None) -> Any:
    """
    返回已初始化的异步 Redis checkpointer。

    须先由 ``init_checkpointer`` 在 lifespan 中建好；未初始化则抛错。
    """
    if _checkpointer is None:
        hint = settings.redis_url if settings is not None else "redis"
        raise RuntimeError(
            f"Redis checkpointer 尚未初始化（{hint}）；"
            "请在 FastAPI lifespan 启动时调用 init_checkpointer"
        )
    if settings is not None and _checkpointer_key != settings.redis_url:
        raise RuntimeError(
            f"Checkpointer 绑定的 Redis（{_checkpointer_key}）与当前配置"
            f"（{settings.redis_url}）不一致；请重启进程"
        )
    return _checkpointer


async def close_checkpointer() -> None:
    """关闭并清空 checkpointer 单例（lifespan 退出时调用）。"""
    global _checkpointer, _checkpointer_key
    async with _checkpointer_lock:
        saver = _checkpointer
        _checkpointer = None
        _checkpointer_key = None
    if saver is not None:
        try:
            await saver.__aexit__(None, None, None)
        except Exception:  # noqa: BLE001
            logger.debug("关闭 AsyncRedisSaver 失败", exc_info=True)


def build_general_purpose_subagent(
    *,
    model: Any,
    specialist_names: list[str],
) -> dict[str, Any]:
    """
    按当前方法论的专业子 Agent 列表，构造显式 ``general-purpose`` 子 Agent，
    传入 ``create_deep_agent(subagents=...)``，避免跨方法论互相污染。
    """
    names = " / ".join(specialist_names) if specialist_names else "专业子 Agent"
    return {
        "name": "general-purpose",
        "description": (
            f"通用兜底子 Agent。仅当 {names} 都不适合时使用；优先选择专业子 Agent。"
        ),
        "system_prompt": (
            "你是通用助手。完成主 Agent 分配的杂项任务后，"
            "返回简洁结论。默认使用简体中文。"
        ),
        "tools": [],
        "model": model,
    }
