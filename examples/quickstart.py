"""
最小可运行示例：不启动 REPL，直接 invoke 一次。

运行前请配置 .env 中的 API Key::

    python examples/quickstart.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from deepagents_app.factory import build_deep_agent


def main() -> None:
    agent = build_deep_agent()
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
