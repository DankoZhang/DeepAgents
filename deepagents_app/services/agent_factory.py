"""
Agent Factory（方法论驱动）
==========================

流程（设计文档 §4.2）：
  methodology_id → 加载 Agent/SubAgent/Tool/Middleware → create_deep_agent()

缓存（设计文档 §8）：
  key = methodology_id + version
  value = Compiled Agent
  服务重启可丢失，支持主动失效。

版本（设计文档 §10）：
  旧会话按 Conversation.methodology_version 从快照重建；
  与当前行一致时直接读 live 表。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session, joinedload

from deepagents_app.backends import build_filesystem_backend
from deepagents_app.config import Settings, get_settings
from deepagents_app.db.models import (
    AgentDefinition,
    Methodology,
    MiddlewareDefinition,
)
from deepagents_app.factory import (
    _build_checkpointer,
    _build_interrupt_on,
    _build_permissions,
    _configure_general_purpose_profile,
    _sync_memory_and_skills_into_workspace,
)
from deepagents_app.models import build_chat_model
from deepagents_app.registries.middleware import load_middleware_object
from deepagents_app.registries.tools import expand_tool_definition, load_tools_by_ids
from deepagents_app.services.revisions import get_revision

logger = logging.getLogger(__name__)

# 进程内 Compiled Agent 缓存
_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def cache_key(methodology_id: str, version: int) -> str:
    return f"{methodology_id}:v{version}"


def invalidate_agent_cache(
    methodology_id: str | None = None,
    version: int | None = None,
) -> int:
    """
    失效缓存。

    - 指定 id+version：删一条
    - 仅指定 id：删该方法论所有版本
    - 都不指定：清空
    """
    with _cache_lock:
        if methodology_id is None:
            n = len(_cache)
            _cache.clear()
            return n
        if version is not None:
            key = cache_key(methodology_id, version)
            return 1 if _cache.pop(key, None) is not None else 0
        prefix = f"{methodology_id}:v"
        to_del = [k for k in _cache if k.startswith(prefix)]
        for k in to_del:
            del _cache[k]
        return len(to_del)


def get_methodology_config(
    db: Session,
    methodology_id: str,
    *,
    version: int | None = None,
) -> Methodology:
    """查询方法论 live 行（含 agents / tools / middlewares）。"""
    methodology = (
        db.query(Methodology)
        .options(
            joinedload(Methodology.agents).joinedload(AgentDefinition.tools),
            joinedload(Methodology.agents).joinedload(AgentDefinition.middlewares),
        )
        .filter(Methodology.id == methodology_id)
        .one_or_none()
    )
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")
    if version is not None and methodology.version != version:
        logger.warning(
            "请求版本 v%s 与当前方法论版本 v%s 不一致，将尝试快照重建",
            version,
            methodology.version,
        )
    return methodology


def _agent_role(agent: AgentDefinition | dict[str, Any]) -> str:
    if isinstance(agent, dict):
        cfg = agent.get("config") or {}
    else:
        cfg = agent.config or {}
    return str(cfg.get("role", "subagent")).lower()


def _agent_enabled(agent: AgentDefinition | dict[str, Any]) -> bool:
    if isinstance(agent, dict):
        cfg = agent.get("config") or {}
    else:
        cfg = agent.config or {}
    return bool(cfg.get("enabled", True))


def _load_middlewares_by_ids(db: Session, middleware_ids: list[str]) -> list[Any]:
    if not middleware_ids:
        return []
    rows = (
        db.query(MiddlewareDefinition)
        .filter(MiddlewareDefinition.id.in_(middleware_ids))
        .all()
    )
    by_id = {r.id: r for r in rows}
    result: list[Any] = []
    for mid in middleware_ids:
        row = by_id.get(mid)
        if row is None:
            logger.warning("快照引用的中间件不存在，跳过：%s", mid)
            continue
        result.append(load_middleware_object(row))
    return result


def _expand_agent_tools(agent: AgentDefinition) -> list[Any]:
    tools: list[Any] = []
    for t in agent.tools:
        tools.extend(expand_tool_definition(t))
    return tools


def _build_subagent_spec_from_row(agent: AgentDefinition) -> dict[str, Any]:
    """AgentDefinition → deepagents SubAgent 字典。"""
    cfg = agent.config or {}
    tools = _expand_agent_tools(agent)
    middleware = [load_middleware_object(m) for m in agent.middlewares]

    spec: dict[str, Any] = {
        "name": agent.name,
        "description": str(cfg.get("description") or agent.name),
        "system_prompt": agent.system_prompt,
        "tools": tools,
    }
    skills = cfg.get("skills")
    if skills:
        spec["skills"] = list(skills)
    if middleware:
        spec["middleware"] = middleware
    if agent.model:
        spec["model"] = agent.model
    return spec


def _build_subagent_spec_from_snapshot(
    db: Session,
    agent: dict[str, Any],
) -> dict[str, Any]:
    cfg = agent.get("config") or {}
    tools = load_tools_by_ids(db, list(agent.get("tool_ids") or []))
    middleware = _load_middlewares_by_ids(db, list(agent.get("middleware_ids") or []))

    spec: dict[str, Any] = {
        "name": agent["name"],
        "description": str(cfg.get("description") or agent["name"]),
        "system_prompt": agent.get("system_prompt") or "",
        "tools": tools,
    }
    skills = cfg.get("skills")
    if skills:
        spec["skills"] = list(skills)
    if middleware:
        spec["middleware"] = middleware
    if agent.get("model"):
        spec["model"] = agent["model"]
    return spec


def _assemble_create_kwargs(
    *,
    settings: Settings,
    supervisor_prompt: str,
    supervisor_name: str,
    supervisor_model: str | None,
    supervisor_temperature: float | None,
    supervisor_tools: list[Any],
    supervisor_middleware: list[Any],
    supervisor_config: dict[str, Any],
    subagents: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    live / snapshot 两条路径共用的 ``create_deep_agent`` 参数组装。

    HITL：方法论可覆盖 ``interrupt_on``；全局开关关闭时强制为 None。
    """
    interrupt_cfg = supervisor_config.get("interrupt_on")
    if interrupt_cfg is None:
        interrupt_on = _build_interrupt_on(settings)
    else:
        interrupt_on = interrupt_cfg if settings.enable_hitl else None

    model = build_chat_model(
        settings,
        model_name=supervisor_model,
        temperature=supervisor_temperature,
    )

    backend = build_filesystem_backend(settings)
    checkpointer = _build_checkpointer(settings)
    permissions = _build_permissions()
    _configure_general_purpose_profile(settings)
    # FilesystemBackend 根 = workspace，需先同步 AGENTS.md / skills
    _sync_memory_and_skills_into_workspace(settings)
    memory_paths = ["/AGENTS.md"] if (settings.workspace_dir / "AGENTS.md").exists() else None

    create_kwargs: dict[str, Any] = {
        "model": model,
        "system_prompt": supervisor_prompt,
        "subagents": subagents,
        "backend": backend,
        "middleware": supervisor_middleware,
        "memory": memory_paths,
        "permissions": permissions,
        "interrupt_on": interrupt_on,
        "checkpointer": checkpointer,
        "name": supervisor_name,
    }
    if supervisor_tools:
        create_kwargs["tools"] = supervisor_tools
    return create_kwargs


def _build_from_live(
    db: Session,
    methodology: Methodology,
    settings: Settings,
) -> Any:
    """从当前 live 表组装 Agent（会话版本 == 方法论当前 version）。"""
    from deepagents import create_deep_agent

    agents = [a for a in methodology.agents if _agent_enabled(a)]
    supervisors = [a for a in agents if _agent_role(a) == "supervisor"]
    subagents_defs = [a for a in agents if _agent_role(a) != "supervisor"]

    if not supervisors:
        raise ValueError(f"方法论 {methodology.id} 缺少 Supervisor Agent")
    if len(supervisors) > 1:
        logger.warning("方法论 %s 有多个 Supervisor，使用第一个", methodology.id)
    supervisor = supervisors[0]

    subagents = [_build_subagent_spec_from_row(a) for a in subagents_defs]
    middleware = [load_middleware_object(m) for m in supervisor.middlewares]
    supervisor_tools = _expand_agent_tools(supervisor)

    logger.info(
        "动态组装 Agent（live）：methodology=%s v%s supervisor=%s subagents=%s",
        methodology.id,
        methodology.version,
        supervisor.name,
        [s["name"] for s in subagents],
    )

    kwargs = _assemble_create_kwargs(
        settings=settings,
        supervisor_prompt=supervisor.system_prompt,
        supervisor_name=supervisor.name,
        supervisor_model=supervisor.model,
        supervisor_temperature=supervisor.temperature,
        supervisor_tools=supervisor_tools,
        supervisor_middleware=middleware,
        supervisor_config=dict(supervisor.config or {}),
        subagents=subagents,
    )
    return create_deep_agent(**kwargs)


def _build_from_snapshot(
    db: Session,
    methodology_id: str,
    version: int,
    settings: Settings,
) -> Any:
    """从 MethodologyRevision 快照组装 Agent（旧会话锁定历史版本）。"""
    from deepagents import create_deep_agent

    revision = get_revision(db, methodology_id, version)
    if revision is None:
        raise LookupError(
            f"方法论快照不存在：{methodology_id} v{version}（无法按旧会话版本重建）"
        )
    snapshot = revision.snapshot or {}
    agents = [a for a in snapshot.get("agents", []) if _agent_enabled(a)]
    supervisors = [a for a in agents if _agent_role(a) == "supervisor"]
    subagents_defs = [a for a in agents if _agent_role(a) != "supervisor"]

    if not supervisors:
        raise ValueError(f"快照 {methodology_id} v{version} 缺少 Supervisor Agent")
    supervisor = supervisors[0]

    subagents = [_build_subagent_spec_from_snapshot(db, a) for a in subagents_defs]
    middleware = _load_middlewares_by_ids(
        db, list(supervisor.get("middleware_ids") or [])
    )
    supervisor_tools = load_tools_by_ids(db, list(supervisor.get("tool_ids") or []))

    logger.info(
        "动态组装 Agent（snapshot）：methodology=%s v%s supervisor=%s subagents=%s",
        methodology_id,
        version,
        supervisor.get("name"),
        [s["name"] for s in subagents],
    )

    kwargs = _assemble_create_kwargs(
        settings=settings,
        supervisor_prompt=supervisor.get("system_prompt") or "",
        supervisor_name=str(supervisor.get("name") or "supervisor"),
        supervisor_model=supervisor.get("model"),
        supervisor_temperature=supervisor.get("temperature"),
        supervisor_tools=supervisor_tools,
        supervisor_middleware=middleware,
        supervisor_config=dict(supervisor.get("config") or {}),
        subagents=subagents,
    )
    return create_deep_agent(**kwargs)


def build_agent_from_methodology(
    db: Session,
    methodology_id: str,
    *,
    version: int | None = None,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> Any:
    """
    根据方法论动态创建 Compiled Agent。

    Returns:
        LangGraph CompiledStateGraph
    """
    settings = settings or get_settings()
    methodology = get_methodology_config(db, methodology_id, version=version)
    # 未指定 version 时跟 live；指定时优先保证会话创建时的版本一致性
    target_version = version if version is not None else methodology.version
    key = cache_key(methodology.id, target_version)

    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                logger.info("命中 Agent 缓存：%s", key)
                return cached

    # 版本一致走 live（含关系预加载）；否则必须从快照重建
    if target_version == methodology.version:
        agent = _build_from_live(db, methodology, settings)
    else:
        agent = _build_from_snapshot(db, methodology.id, target_version, settings)

    if use_cache:
        with _cache_lock:
            _cache[key] = agent
    return agent
