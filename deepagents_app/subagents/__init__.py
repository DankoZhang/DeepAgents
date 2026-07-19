"""
子 Agent 规格总入口
==================

每个子 Agent 是一份符合 deepagents ``SubAgent`` 规范的字典。

规格来源：``deepagents_app/config/subagents.yaml``（可通过 Settings.subagents_config 覆盖）。
构建 graph 时调用 ``build_all_subagents()`` 自动加载并解析。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from deepagents_app.subagents.loader import load_subagents_from_yaml


def build_all_subagents(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    """
    从 YAML 构建全部同步子 Agent 规格。

    Args:
        config_path: 可选 YAML 路径；默认读 ``Settings.subagents_config`` /
            ``deepagents_app/config/subagents.yaml``。
    """
    if config_path is None:
        try:
            from deepagents_app.config import get_settings

            config_path = get_settings().subagents_config
        except Exception:  # noqa: BLE001
            config_path = None
    return load_subagents_from_yaml(config_path)


__all__ = ["build_all_subagents", "load_subagents_from_yaml"]
