"""
Agent 角色校验
==============

发布与组装共用：enabled Agent 中须恰好一个 Supervisor。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TypeVar

from deepagents_app.api.errors import BusinessError

T = TypeVar("T")


def require_single_supervisor(
    agents: Sequence[T],
    *,
    context: str,
    role_of: Callable[[T], str],
    name_of: Callable[[T], str],
) -> tuple[T, list[T]]:
    """
    从 ``agents`` 中拆出唯一 Supervisor 与其余 SubAgent。

    ``role_of`` 应返回小写角色名（``supervisor`` / ``subagent`` 等）。
    """
    supervisors = [a for a in agents if role_of(a) == "supervisor"]
    others = [a for a in agents if role_of(a) != "supervisor"]
    if not supervisors:
        raise BusinessError(f"{context}：缺少 Supervisor Agent")
    if len(supervisors) > 1:
        names = ", ".join(name_of(a) for a in supervisors)
        raise BusinessError(
            f"{context}：只能有一个 Supervisor Agent，当前：{names}"
        )
    return supervisors[0], others
