"""API 包：FastAPI 路由与 schemas。"""

from __future__ import annotations

__all__ = ["app", "create_app"]


def __getattr__(name: str):
    if name in {"app", "create_app"}:
        from deepagents_app.api.app import app, create_app

        return app if name == "app" else create_app
    raise AttributeError(name)
