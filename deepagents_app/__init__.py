"""
DeepAgents 方法论平台
====================

基于 LangChain ``deepagents`` 的可配置多 Agent 后端。

快速开始（需数据库；按用户引导种子）::

    from deepagents_app.db.seed import ensure_user_bootstrap
    from deepagents_app.db.session import get_session_factory, migrate_db
    from deepagents_app.ownership import demo_methodology_id_for_user
    from deepagents_app.services.agent_factory import build_agent_from_methodology

    migrate_db()  # 或: python -m deepagents_app.db.migrate
    user_id = "cli-user"
    db = get_session_factory()()
    ensure_user_bootstrap(db, user_id)
    db.commit()
    mid = demo_methodology_id_for_user(user_id)
    agent = build_agent_from_methodology(db, mid, owner_user_id=user_id)
"""

from __future__ import annotations

from typing import Any

__all__ = ["build_agent_from_methodology"]
__version__ = "0.2.0"


def __getattr__(name: str) -> Any:
    if name == "build_agent_from_methodology":
        from deepagents_app.services.agent_factory import build_agent_from_methodology

        return build_agent_from_methodology
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
