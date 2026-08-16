#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   server.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   server.py

DeepAgents FastAPI 服务入口
==========================

用法::

    # 先启动基础设施
    docker compose up -d

    # 启动 API（API_WORKERS / API_SERVER 见 .env）
    python server.py

    # 等价手写：
    # uvicorn deepagents_app.api.app:app --host 0.0.0.0 --port 8001 --workers 4
    # gunicorn deepagents_app.api.app:app -k uvicorn.workers.UvicornWorker -w 4 -b 0.0.0.0:8001
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from deepagents_app.config import get_settings

    settings = get_settings()
    workers = int(settings.api_workers)
    host = settings.api_host
    port = int(settings.api_port)
    server = (settings.api_server or "uvicorn").strip().lower()
    # Windows 默认 ProactorEventLoop 与 psycopg 异步不兼容；uvicorn 0.36+
    # 会显式选用 Proactor，需改为 SelectorEventLoop。
    loop = "asyncio:SelectorEventLoop" if sys.platform == "win32" else "auto"

    # gunicorn 依赖 fcntl，仅 Unix 可用；Windows 自动回退到 uvicorn。
    if server == "gunicorn" and sys.platform == "win32":
        print(
            "WARNING: API_SERVER=gunicorn 在 Windows 上不可用（无 fcntl），已回退到 uvicorn",
            file=sys.stderr,
        )
        server = "uvicorn"

    if server == "gunicorn":
        from gunicorn.app.base import BaseApplication

        class _App(BaseApplication):
            def __init__(self, options: dict) -> None:
                self.options = options
                super().__init__()

            def load_config(self) -> None:
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                from deepagents_app.api.app import app

                return app

        _App(
            {
                "bind": f"{host}:{port}",
                "workers": workers,
                "worker_class": "uvicorn.workers.UvicornWorker",
                "timeout": 120,
                "graceful_timeout": 30,
                "keepalive": 5,
            }
        ).run()
        return

    import uvicorn

    uvicorn.run(
        "deepagents_app.api.app:app",
        host=host,
        port=port,
        workers=workers,
        loop=loop,
        reload=False,
    )


if __name__ == "__main__":
    main()
