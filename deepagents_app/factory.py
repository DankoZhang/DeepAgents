"""
Deep Agent 共享组装工具
======================

供 ``agent_factory`` 复用的底层能力：
checkpointer / HITL / permissions / general-purpose 子 Agent 规格 / workspace 同步。

方法论驱动的完整 Agent 组装见 ``deepagents_app.services.agent_factory``。
HarnessProfile **不再**全局注册：按方法论在组装时显式注入 ``general-purpose`` 子 Agent。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from deepagents import FilesystemPermission
from langgraph.checkpoint.memory import InMemorySaver

from deepagents_app.config import Settings

logger = logging.getLogger(__name__)

_checkpointer: Any | None = None
_checkpointer_key: tuple[str, bool] | None = None
_checkpointer_lock = threading.Lock()

__all__ = [
    "build_permissions",
    "build_interrupt_on",
    "build_checkpointer",
    "build_general_purpose_subagent",
    "sync_memory_into_workspace",
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


def build_interrupt_on(settings: Settings) -> dict[str, bool] | None:
    """危险工具人工审批配置；关闭 HITL 时返回 None。"""
    if not settings.enable_hitl:
        return None
    return {
        "run_shell_command": True,
        "write_workspace_file": True,
        "write_file": True,
        "edit_file": True,
        "execute": True,
    }


def build_checkpointer(settings: Settings):
    """
    构建（或复用）checkpointer，用于多轮对话的 thread 级状态持久化。

    同一进程内对相同 ``redis_url`` / ``require_redis_checkpointer`` 复用单例。
    """
    global _checkpointer, _checkpointer_key

    key = (settings.redis_url, bool(settings.require_redis_checkpointer))
    with _checkpointer_lock:
        if _checkpointer is not None and _checkpointer_key == key:
            return _checkpointer

        try:
            from langgraph.checkpoint.redis import RedisSaver

            saver = RedisSaver(redis_url=settings.redis_url)
            saver.setup()
            logger.info("Checkpointer: RedisSaver -> %s", settings.redis_url)
        except Exception as exc:  # noqa: BLE001
            if settings.require_redis_checkpointer:
                raise RuntimeError(
                    f"Redis checkpointer 不可用（REQUIRE_REDIS_CHECKPOINTER=true）：{exc}"
                ) from exc
            logger.warning(
                "使用 InMemorySaver（Redis 不可用：%s）。"
                "多 worker / 重启后对话状态会丢失；生产请设 REQUIRE_REDIS_CHECKPOINTER=true",
                exc,
            )
            saver = InMemorySaver()

        _checkpointer = saver
        _checkpointer_key = key
        return saver


def build_general_purpose_subagent(
    *,
    model: Any,
    specialist_names: list[str],
) -> dict[str, Any]:
    """
    按当前方法论的专业子 Agent 列表，构造显式 ``general-purpose`` 子 Agent。

    传入 ``create_deep_agent(subagents=...)`` 后，deepagents 不会再按全局
    HarnessProfile 自动注入同名兜底 Agent，从而避免跨方法论互相污染。
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


def sync_memory_into_workspace(settings: Settings) -> None:
    """
    把项目级 AGENTS.md 同步到**全局** workspace 根（兼容旧路径）。

    用户隔离工作区由 ``workspace.ensure_user_workspace`` 在组装时按用户同步。
    """
    import shutil

    src_memory = settings.memory_file
    dst_memory = settings.workspace_dir / "AGENTS.md"
    if src_memory.exists():
        shutil.copy2(src_memory, dst_memory)
