"""
Tool Registry
=============

数据库存元信息；运行时按 ``class_path`` 动态加载 Python 对象。
``class_path`` 格式：``module.path:attr_name``
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.db.models import ToolDefinition

logger = logging.getLogger(__name__)


def resolve_class_path(class_path: str) -> Any:
    """``module.path:attr`` → 对象。"""
    if ":" not in class_path:
        raise ValueError(f"非法 class_path（需要 module:attr）：{class_path}")
    module_name, attr_name = class_path.split(":", 1)
    module = importlib.import_module(module_name)
    try:
        return getattr(module, attr_name)
    except AttributeError as exc:
        raise ImportError(f"无法从 {module_name} 加载 {attr_name}") from exc


def load_tool_object(tool_def: ToolDefinition) -> Any:
    """加载单个工具实例；支持可调用工厂。"""
    if tool_def.status != "active":
        raise ValueError(f"工具已禁用：{tool_def.name}")
    obj = resolve_class_path(tool_def.class_path)
    if callable(obj) and not hasattr(obj, "name") and not hasattr(obj, "invoke"):
        # 无参工厂（少见）；LangChain @tool 对象有 name/invoke，直接返回
        cfg = tool_def.config or {}
        if cfg.get("instantiate"):
            return obj(**{k: v for k, v in cfg.items() if k != "instantiate"})
    return obj


def load_tools_by_ids(db: Session, tool_ids: list[str]) -> list[Any]:
    """按 id 列表加载工具，保持顺序、去重。"""
    if not tool_ids:
        return []
    rows = (
        db.query(ToolDefinition)
        .filter(ToolDefinition.id.in_(tool_ids), ToolDefinition.status == "active")
        .all()
    )
    by_id = {r.id: r for r in rows}
    tools: list[Any] = []
    seen: set[str] = set()
    for tid in tool_ids:
        if tid in seen:
            continue
        seen.add(tid)
        row = by_id.get(tid)
        if row is None:
            logger.warning("工具不存在或未激活，跳过：%s", tid)
            continue
        tools.append(load_tool_object(row))
    return tools


def load_tools_for_agent(db: Session, agent_id: str) -> list[Any]:
    """加载 Agent 绑定的全部工具。"""
    from deepagents_app.db.models import AgentDefinition

    agent = db.get(AgentDefinition, agent_id)
    if agent is None:
        return []
    return [load_tool_object(t) for t in agent.tools if t.status == "active"]
