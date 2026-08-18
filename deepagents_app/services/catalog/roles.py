#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   roles.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   roles.py

Agent 角色校验
==============

发布与组装共用：仅统计 enabled Agent，其中须恰好一个 Supervisor。
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
    enabled_of: Callable[[T], bool],
) -> tuple[T, list[T]]:
    """
    从 enabled Agent 中拆出唯一 Supervisor 与其余 SubAgent。

    发布与组装必须传入同一套 ``role_of`` / ``enabled_of``，避免门禁分叉。
    ``role_of`` 返回小写角色名（``supervisor`` / ``subagent``）。
    """
    pool = [a for a in agents if enabled_of(a)]
    supervisors = [a for a in pool if role_of(a) == "supervisor"]
    others = [a for a in pool if role_of(a) != "supervisor"]
    if not supervisors:
        raise BusinessError(f"{context}：缺少启用中的 Supervisor Agent")
    if len(supervisors) > 1:
        names = ", ".join(name_of(a) for a in supervisors)
        raise BusinessError(
            f"{context}：启用中只能有一个 Supervisor Agent，当前：{names}"
        )
    return supervisors[0], others
