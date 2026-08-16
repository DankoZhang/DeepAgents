#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   loading.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   loading.py

ORM 预加载选项与方法论详情查询（避免 services 间重复 load 链）。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from deepagents_app.db.models import AgentDefinition, Methodology


def _methodology_with_agents_options():
    """方法论详情：agents + tools / middlewares / skills / llm_model。

    集合关系用 selectinload，避免多集合 joinedload 笛卡尔积；
    llm_model 为 many-to-one，可用 joinedload。
    """
    return (
        selectinload(Methodology.agents).selectinload(AgentDefinition.tools),
        selectinload(Methodology.agents).selectinload(AgentDefinition.middlewares),
        selectinload(Methodology.agents).selectinload(AgentDefinition.skills),
        selectinload(Methodology.agents).joinedload(AgentDefinition.llm_model),
    )


async def load_methodology_with_agents(
    db: AsyncSession,
    methodology_id: str,
    *,
    owner_user_id: str | None = None,
) -> Methodology | None:
    """带 agents / tools / middlewares / skills / llm 预加载的方法论；不存在返回 None。"""
    stmt = (
        select(Methodology)
        .options(*_methodology_with_agents_options())
        .where(Methodology.id == methodology_id)
    )
    if owner_user_id is not None:
        stmt = stmt.where(Methodology.owner_user_id == owner_user_id)
    return (await db.scalars(stmt)).one_or_none()


def agent_detail_options():
    """单个 Agent 详情：tools / middlewares / skills / llm_model。"""
    return (
        selectinload(AgentDefinition.tools),
        selectinload(AgentDefinition.middlewares),
        selectinload(AgentDefinition.skills),
        joinedload(AgentDefinition.llm_model),
    )
