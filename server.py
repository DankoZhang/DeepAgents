#!/usr/bin/env python3
"""
DeepAgents FastAPI 服务入口
==========================

用法::

    # 先启动基础设施
    docker compose up -d

    # 启动 API
    python server.py
    # 或
    uvicorn deepagents_app.api.app:app --host 0.0.0.0 --port 8001 --reload
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    import uvicorn

    from deepagents_app.config import get_settings

    settings = get_settings()
    uvicorn.run(
        "deepagents_app.api.app:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
