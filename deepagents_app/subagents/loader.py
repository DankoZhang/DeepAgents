"""
从 YAML 加载 SubAgent 规格
==========================

构建 graph 时读取配置文件，解析 tools / middleware / system_prompt，
产出符合 deepagents ``SubAgent`` 规范的字典列表。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from deepagents_app.subagents.registry import resolve_middleware, resolve_tools

logger = logging.getLogger(__name__)

# 默认配置：与本包同级的 config/subagents.yaml
_DEFAULT_CONFIG = (
    Path(__file__).resolve().parent.parent / "config" / "subagents.yaml"
)


def load_subagents_from_yaml(config_path: str | Path | None = None) -> list[dict[str, Any]]:
    """
    加载并解析 SubAgent YAML 配置。

    Args:
        config_path: YAML 路径；``None`` 时使用默认 ``deepagents_app/config/subagents.yaml``。

    Returns:
        deepagents ``subagents=`` 可直接消费的规格字典列表（已跳过 ``enabled: false``）。
    """
    path = Path(config_path) if config_path else _DEFAULT_CONFIG
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"SubAgent 配置不存在：{path}")

    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    entries = raw.get("subagents")
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"配置无效：{path} 中缺少非空 subagents 列表")

    base_dir = path.parent
    subagents: list[dict[str, Any]] = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"subagents[{idx}] 必须是映射对象")
        if entry.get("enabled", True) is False:
            logger.info("跳过已禁用 SubAgent：%s", entry.get("name", f"#{idx}"))
            continue
        subagents.append(_build_subagent_spec(entry, base_dir=base_dir, index=idx))

    if not subagents:
        raise ValueError(f"配置 {path} 中没有启用的 SubAgent")

    names = [s["name"] for s in subagents]
    if len(names) != len(set(names)):
        raise ValueError(f"SubAgent name 必须唯一，当前：{names}")

    logger.info("已从 %s 加载 %d 个 SubAgent：%s", path, len(subagents), names)
    return subagents


def _build_subagent_spec(
    entry: dict[str, Any],
    *,
    base_dir: Path,
    index: int,
) -> dict[str, Any]:
    """单条 YAML 条目 → SubAgent 规格字典。"""
    name = entry.get("name")
    if not name or not isinstance(name, str):
        raise ValueError(f"subagents[{index}] 缺少有效 name")

    description = entry.get("description")
    if not description or not isinstance(description, str):
        raise ValueError(f"SubAgent '{name}' 缺少 description")

    system_prompt = _resolve_system_prompt(entry, base_dir=base_dir, name=name)

    spec: dict[str, Any] = {
        "name": name.strip(),
        "description": description.strip(),
        "system_prompt": system_prompt,
        "tools": resolve_tools(entry.get("tools")),
    }

    skills = entry.get("skills")
    if skills:
        if not isinstance(skills, list):
            raise ValueError(f"SubAgent '{name}' 的 skills 必须是列表")
        spec["skills"] = [str(s) for s in skills]

    middleware = resolve_middleware(entry.get("middleware"))
    if middleware:
        spec["middleware"] = middleware

    model = entry.get("model")
    if model:
        spec["model"] = model

    return spec


def _resolve_system_prompt(entry: dict[str, Any], *, base_dir: Path, name: str) -> str:
    """优先 system_prompt_file，否则使用内联 system_prompt。"""
    prompt_file = entry.get("system_prompt_file")
    inline = entry.get("system_prompt")

    if prompt_file:
        path = Path(prompt_file)
        if not path.is_absolute():
            path = (base_dir / path).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"SubAgent '{name}' 的 system_prompt_file 不存在：{path}")
        return path.read_text(encoding="utf-8").strip()

    if inline and isinstance(inline, str) and inline.strip():
        return inline.strip()

    raise ValueError(
        f"SubAgent '{name}' 需提供 system_prompt 或 system_prompt_file"
    )
