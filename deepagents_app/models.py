"""兼容旧导入路径：请改用 ``deepagents_app.llm``。"""

from __future__ import annotations

from deepagents_app.llm import (  # noqa: F401
    build_chat_model,
    build_chat_model_from_spec,
    model_spec_from_row,
)

__all__ = [
    "build_chat_model",
    "build_chat_model_from_spec",
    "model_spec_from_row",
]
