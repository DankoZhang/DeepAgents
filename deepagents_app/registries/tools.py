"""
Tool Registry
=============

- builtin：按 class_path 动态 import
- mcp：按 config 连接 MCP Server，展开为 LangChain tools
- mcp 工具列表按 tool_id+config 指纹进程内缓存，避免每次组装都重连
- 配置变更经 Redis pub/sub 跨 worker 失效（见 ``invalidate_mcp_tools_cache``）
- MCP 加载走当前事件循环的 async 路径
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import threading
from typing import Any

from deepagents_app.db.models import ToolDefinition

logger = logging.getLogger(__name__)

# tool_id → (config_fingerprint, tools)
_mcp_tools_cache: dict[str, tuple[str, list[Any]]] = {}
_mcp_tools_lock = threading.Lock()


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


def _mcp_config_fingerprint(tool_def: ToolDefinition) -> str:
    payload = {
        "id": tool_def.id,
        "name": tool_def.name,
        "status": tool_def.status,
        "config": tool_def.config or {},
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clear_mcp_tools_cache(*, tool_id: str | None = None) -> None:
    """仅清除本进程 MCP 工具缓存；``tool_id`` 为空则清空全部。"""
    with _mcp_tools_lock:
        if tool_id is None:
            _mcp_tools_cache.clear()
            return
        _mcp_tools_cache.pop(tool_id, None)


def invalidate_mcp_tools_cache(*, tool_id: str | None = None) -> None:
    """本进程清除后，经 Redis pub/sub 通知其他 worker。"""
    clear_mcp_tools_cache(tool_id=tool_id)
    try:
        from deepagents_app.services.infra.cache_pubsub import publish_mcp_cache_invalidation

        publish_mcp_cache_invalidation(
            tool_id=tool_id,
            all_keys=tool_id is None,
        )
    except Exception:  # noqa: BLE001
        logger.warning("广播 MCP 缓存失效失败", exc_info=True)


async def _aload_mcp_tools(tool_def: ToolDefinition) -> list[Any]:
    from langchain_mcp_adapters.client import MultiServerMCPClient

    from deepagents_app.utils.mcp_safety import validate_mcp_config

    cfg = validate_mcp_config(dict(tool_def.config or {}))
    server_name = tool_def.name
    client = MultiServerMCPClient({server_name: _mcp_connection_from_config(cfg)})
    tools = await client.get_tools(server_name=server_name)
    include = cfg.get("include_tools")
    if include:
        allow = set(include)
        tools = [t for t in tools if getattr(t, "name", None) in allow]
    return list(tools)


async def load_mcp_tools(tool_def: ToolDefinition) -> list[Any]:
    """加载 MCP Server 下的工具列表（同配置进程内复用，避免反复建连）。"""
    if tool_def.status != "active":
        raise ValueError(f"工具已禁用：{tool_def.name}")
    tool_id = str(tool_def.id or tool_def.name)
    fingerprint = _mcp_config_fingerprint(tool_def)
    with _mcp_tools_lock:
        hit = _mcp_tools_cache.get(tool_id)
        if hit is not None and hit[0] == fingerprint:
            return list(hit[1])
    try:
        tools = await _aload_mcp_tools(tool_def)
    except Exception:
        logger.exception("加载 MCP 工具失败：%s", tool_def.name)
        raise
    with _mcp_tools_lock:
        _mcp_tools_cache[tool_id] = (fingerprint, tools)
    return list(tools)


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


async def expand_tool_definition(tool_def: ToolDefinition) -> list[Any]:
    """一条 ToolDefinition → 0..N 个可执行 LangChain tool。"""
    if tool_def.status != "active":
        return []
    if (tool_def.tool_type or "builtin") == "mcp":
        return await load_mcp_tools(tool_def)
    return [load_builtin_tool(tool_def)]


def tool_definition_from_snapshot(payload: dict[str, Any]) -> ToolDefinition:
    """从快照 dict 构造脱离 Session 的 ToolDefinition（仅供运行时展开）。"""
    return ToolDefinition(
        id=str(payload.get("id") or payload.get("name") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        tool_type=str(payload.get("tool_type") or "builtin"),
        class_path=payload.get("class_path"),
        requires_hitl=bool(payload.get("requires_hitl", False)),
        config=dict(payload.get("config") or {}),
        status=str(payload.get("status") or "active"),
    )


async def interrupt_tool_names_from_payloads(
    payloads: list[dict[str, Any]],
) -> dict[str, bool]:
    """
    从工具快照/内嵌 payload 收集 requires_hitl 对应的运行时工具名。

    - builtin：使用 ToolDefinition.name（与 LangChain tool.name 一致）
    - mcp：优先 config.include_tools；否则展开 MCP 后取各工具名
    """
    names: dict[str, bool] = {}
    for payload in payloads:
        if not payload.get("requires_hitl"):
            continue
        if str(payload.get("status") or "active") != "active":
            continue
        tool_type = str(payload.get("tool_type") or "builtin")
        if tool_type == "mcp":
            cfg = dict(payload.get("config") or {})
            include = cfg.get("include_tools")
            if include:
                for name in include:
                    key = str(name).strip()
                    if key:
                        names[key] = True
                continue
            try:
                expanded = await expand_tool_definition(
                    tool_definition_from_snapshot(payload)
                )
            except Exception:  # noqa: BLE001
                logger.exception(
                    "收集 HITL 工具名时展开 MCP 失败：%s",
                    payload.get("name"),
                )
                continue
            for tool in expanded:
                key = getattr(tool, "name", None)
                if key:
                    names[str(key)] = True
        else:
            key = str(payload.get("name") or "").strip()
            if key:
                names[key] = True
    return names


async def load_tools_from_snapshots(payloads: list[dict[str, Any]]) -> list[Any]:
    """按快照内嵌的工具 payload 展开（顺序保留、按 id 去重）。"""
    tools: list[Any] = []
    seen: set[str] = set()
    for payload in payloads:
        tid = str(payload.get("id") or payload.get("name") or "")
        if tid and tid in seen:
            continue
        if tid:
            seen.add(tid)
        tools.extend(await expand_tool_definition(tool_definition_from_snapshot(payload)))
    return tools
