"""ORM 预加载选项（避免 services 间重复 joinedload 链）。"""

from __future__ import annotations

from sqlalchemy.orm import joinedload

from deepagents_app.db.models import AgentDefinition, Methodology


def methodology_with_agents_options():
    """方法论详情：agents + tools / middlewares / skills / llm_model。"""
    return (
        joinedload(Methodology.agents).joinedload(AgentDefinition.tools),
        joinedload(Methodology.agents).joinedload(AgentDefinition.middlewares),
        joinedload(Methodology.agents).joinedload(AgentDefinition.skills),
        joinedload(Methodology.agents).joinedload(AgentDefinition.llm_model),
    )


def agent_detail_options():
    """单个 Agent 详情：tools / middlewares / skills / llm_model。"""
    return (
        joinedload(AgentDefinition.tools),
        joinedload(AgentDefinition.middlewares),
        joinedload(AgentDefinition.skills),
        joinedload(AgentDefinition.llm_model),
    )
