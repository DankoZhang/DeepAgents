"""
最小可运行示例：按种子方法论组装 Agent 并 invoke 一次。

运行前请配置 .env，并启动 docker compose（PostgreSQL + Redis）::

    python examples/quickstart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deepagents_app.db.seed import seed_defaults
from deepagents_app.db.session import get_session_factory, init_db
from deepagents_app.services.agent_factory import build_agent_from_methodology


def main() -> None:
    init_db()
    db = get_session_factory()()
    try:
        seed_defaults(db)
        db.commit()
        agent = build_agent_from_methodology(db, "demo_deepagents")
    finally:
        db.close()

    result = agent.invoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "请用 qa-expert 解释：Deep Agents 里 Memory 和 Skills 有什么区别？",
                }
            ]
        },
        config={"configurable": {"thread_id": "quickstart-1"}},
    )
    messages = result.get("messages") or []
    last = messages[-1] if messages else None
    content = getattr(last, "content", None) if last is not None else None
    print("=" * 60)
    print(content or result)


if __name__ == "__main__":
    main()
