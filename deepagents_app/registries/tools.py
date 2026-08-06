"""
Tool Registry
=============

- builtin：按 class_path 动态 import
- mcp：按 config 连接 MCP Server，展开为 LangChain tools（并补 sync 包装）
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import importlib
import logging
from typing import Any

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


def _run_coro(coro: Any) -> Any:
    """在同步上下文中跑完 coroutine（兼容已有事件循环）。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ensure_sync_tool(tool: Any) -> Any:
    """MCP StructuredTool 常只有 coroutine；为 sync invoke 补 func。"""
    if getattr(tool, "func", None) is not None:
        return tool
    coro_fn = getattr(tool, "coroutine", None)
    if coro_fn is None:
        return tool

    def _sync(*args: Any, **kwargs: Any) -> Any:
        return _run_coro(coro_fn(*args, **kwargs))

    try:
        tool.func = _sync
    except Exception:  # noqa: BLE001
        logger.debug("无法为工具 %s 注入 sync func", getattr(tool, "name", tool))
    return tool


def _mcp_connection_from_config(cfg: dict[str, Any]) -> dict[str, Any]:
    transport = str(cfg.get("transport") or "stdio")
    conn: dict[str, Any] = {"transport": transport}
    if transport == "stdio":
        conn["command"] = cfg["command"]
        if cfg.get("args"):
            conn["args"] = list(cfg["args"])
        if cfg.get("env"):
            conn["env"] = dict(cfg["env"])
    else:
        conn["url"] = cfg["url"]
        if cfg.get("headers"):
            conn["headers"] = dict(cfg["headers"])
        if cfg.get("env"):
            conn["env"] = dict(cfg["env"])
    return conn


async def _aload_mcp_tools(tool_def: ToolDefinition) -> list[Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    cfg = dict(tool_def.config or {})
    server_name = tool_def.name
    client = MultiServerMCPClient({server_name: _mcp_connection_from_config(cfg)})
    tools = await client.get_tools(server_name=server_name)
    include = cfg.get("include_tools")
    if include:
        allow = set(include)
        tools = [t for t in tools if getattr(t, "name", None) in allow]
    return [_ensure_sync_tool(t) for t in tools]


def load_mcp_tools(tool_def: ToolDefinition) -> list[Any]:
    """加载 MCP Server 下的工具列表。"""
    if tool_def.status != "active":
        raise ValueError(f"工具已禁用：{tool_def.name}")
    try:
        return _run_coro(_aload_mcp_tools(tool_def))
    except Exception:
        logger.exception("加载 MCP 工具失败：%s", tool_def.name)
        raise


def load_builtin_tool(tool_def: ToolDefinition) -> Any:
    """加载单个内置工具实例。"""
    if tool_def.status != "active":
        raise ValueError(f"工具已禁用：{tool_def.name}")
    if not tool_def.class_path:
        raise ValueError(f"内置工具缺少 class_path：{tool_def.name}")
    obj = resolve_class_path(tool_def.class_path)
    if callable(obj) and not hasattr(obj, "name") and not hasattr(obj, "invoke"):
        cfg = tool_def.config or {}
        if cfg.get("instantiate"):
            return obj(**{k: v for k, v in cfg.items() if k != "instantiate"})
    return obj


def expand_tool_definition(tool_def: ToolDefinition) -> list[Any]:
    """一条 ToolDefinition → 0..N 个可执行 LangChain tool。"""
    if tool_def.status != "active":
        return []
    if (tool_def.tool_type or "builtin") == "mcp":
        return load_mcp_tools(tool_def)
    return [load_builtin_tool(tool_def)]


def tool_definition_from_snapshot(payload: dict[str, Any]) -> ToolDefinition:
    """从快照 dict 构造脱离 Session 的 ToolDefinition（仅供运行时展开）。"""
    return ToolDefinition(
        id=str(payload.get("id") or payload.get("name") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        tool_type=str(payload.get("tool_type") or "builtin"),
        class_path=payload.get("class_path"),
        input_schema=payload.get("input_schema"),
        output_schema=payload.get("output_schema"),
        config=dict(payload.get("config") or {}),
        status=str(payload.get("status") or "active"),
    )


def load_tools_from_snapshots(payloads: list[dict[str, Any]]) -> list[Any]:
    """按快照内嵌的工具 payload 展开（顺序保留、按 id 去重）。"""
    tools: list[Any] = []
    seen: set[str] = set()
    for payload in payloads:
        tid = str(payload.get("id") or payload.get("name") or "")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        tools.extend(expand_tool_definition(tool_definition_from_snapshot(payload)))
    return tools
