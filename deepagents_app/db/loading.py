"""ORM 预加载选项（避免 services 间重复 load 链）。"""

from __future__ import annotations

from sqlalchemy.orm import joinedload, selectinload

from deepagents_app.db.models import AgentDefinition, Methodology


def methodology_with_agents_options():
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


def agent_detail_options():
    """单个 Agent 详情：tools / middlewares / skills / llm_model。"""
    return (
        selectinload(AgentDefinition.tools),
        selectinload(AgentDefinition.middlewares),
        selectinload(AgentDefinition.skills),
        joinedload(AgentDefinition.llm_model),
    )
