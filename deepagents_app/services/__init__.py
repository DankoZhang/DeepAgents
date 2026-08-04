"""
业务服务层包入口
================

对外再导出常用符号；路由一般直接 ``from deepagents_app.services import xxx as svc``。
延迟导入，避免 ``import deepagents_app.services`` 立刻拉齐 SQLAlchemy / deepagents。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "build_agent_from_methodology",
    "invalidate_agent_cache",
    "create_conversation",
    "run_chat",
]


def __getattr__(name: str) -> Any:
    if name in {"build_agent_from_methodology", "invalidate_agent_cache"}:
        from deepagents_app.services import agent_factory as _af

        return getattr(_af, name)
    if name == "create_conversation":
        from deepagents_app.services.conversation import create_conversation

        return create_conversation
    if name == "run_chat":
        from deepagents_app.services.chat import chat as run_chat

        return run_chat
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
