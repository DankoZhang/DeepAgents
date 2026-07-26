"""
DeepAgents 方法论平台
====================

基于 LangChain ``deepagents`` 的可配置多 Agent 后端。

快速开始（需数据库与种子方法论）::

    from deepagents_app.db.seed import seed_defaults
    from deepagents_app.db.session import get_session_factory, init_db
    from deepagents_app.services.agent_factory import build_agent_from_methodology

    init_db()
    db = get_session_factory()()
    seed_defaults(db)
    db.commit()
    agent = build_agent_from_methodology(db, "demo_deepagents")
"""

from deepagents_app.services.agent_factory import build_agent_from_methodology

__all__ = ["build_agent_from_methodology"]
__version__ = "0.2.0"
