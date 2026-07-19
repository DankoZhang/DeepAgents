"""
SubAgent 可解析资源注册表
========================

YAML 里用字符串引用工具包 / 中间件，运行时由此映射为真实对象。
新增工具包或中间件时，在此登记即可被配置文件引用。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from deepagents_app.middleware import AuditMiddleware, LoggingMiddleware, TimingMiddleware
from deepagents_app.tools import COMPUTER_TOOLS, DOCUMENT_TOOLS, QA_TOOLS

# 工具包：YAML ``tools: document`` / ``tools: [document, qa]``
TOOL_KITS: dict[str, Sequence[Any]] = {
    "document": DOCUMENT_TOOLS,
    "document_tools": DOCUMENT_TOOLS,
    "computer": COMPUTER_TOOLS,
    "computer_tools": COMPUTER_TOOLS,
    "qa": QA_TOOLS,
    "qa_tools": QA_TOOLS,
}

# 中间件：YAML ``middleware: [logging, timing]``
# value 为无参工厂，每次构建新实例，避免跨 Agent 共享状态
MIDDLEWARE_REGISTRY: dict[str, Callable[[], Any]] = {
    "logging": LoggingMiddleware,
    "timing": TimingMiddleware,
    "audit": AuditMiddleware,
}


def resolve_tools(spec: str | Sequence[str] | None) -> list[Any]:
    """把 YAML 中的 tools 字段解析为工具对象列表。"""
    if spec is None:
        return []
    names = [spec] if isinstance(spec, str) else list(spec)
    tools: list[Any] = []
    seen: set[int] = set()
    for name in names:
        key = str(name).strip()
        if not key:
            continue
        if key not in TOOL_KITS:
            known = ", ".join(sorted(TOOL_KITS))
            raise KeyError(f"未知工具包 '{key}'，可选：{known}")
        for tool in TOOL_KITS[key]:
            tool_id = id(tool)
            if tool_id not in seen:
                seen.add(tool_id)
                tools.append(tool)
    return tools


def resolve_middleware(spec: Sequence[str] | None) -> list[Any]:
    """把 YAML 中的 middleware 字段解析为中间件实例列表。"""
    if not spec:
        return []
    result: list[Any] = []
    for name in spec:
        key = str(name).strip().lower()
        if key not in MIDDLEWARE_REGISTRY:
            known = ", ".join(sorted(MIDDLEWARE_REGISTRY))
            raise KeyError(f"未知中间件 '{name}'，可选：{known}")
        result.append(MIDDLEWARE_REGISTRY[key]())
    return result
