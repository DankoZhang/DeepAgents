"""
Agent Factory（方法论驱动）
==========================

流程：
  methodology_id → 加载 Agent/SubAgent/Tool/Middleware → create_deep_agent()

缓存：
  key = methodology_id + version
  value = Compiled Agent

版本：
  旧会话按 Conversation.methodology_version 从快照重建；
  与当前行一致时直接读 live 表。
  live / snapshot 先归一成统一 Agent 规格，再走同一条组装路径。
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.backends import build_filesystem_backend
from deepagents_app.config import Settings, get_settings
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Methodology, SkillDefinition
from deepagents_app.factory import (
    build_checkpointer,
    build_interrupt_on,
    build_permissions,
    configure_general_purpose_profile,
    sync_memory_into_workspace,
)
from deepagents_app.llm import build_chat_model_from_spec
from deepagents_app.registries.middleware import (
    load_middleware_object,
    load_middlewares_by_ids,
)
from deepagents_app.registries.tools import expand_tool_definition, load_tools_by_ids
from deepagents_app.services.llm_models import resolve_model_spec_for_agent
from deepagents_app.services.revisions import get_revision

logger = logging.getLogger(__name__)

_cache: dict[str, Any] = {}
_cache_lock = threading.Lock()


def cache_key(methodology_id: str, version: int) -> str:
    """生成缓存键：方法论 id + 版本号。"""
    return f"{methodology_id}:v{version}"


def skills_scope(methodology_id: str, version: int) -> str:
    """Skills 物化目录隔离键，避免并发组装互删。"""
    return f"{methodology_id}/v{version}"


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
    """查询方法论 live 行（含 agents / tools / middlewares / skills / llm）。"""
    methodology = (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
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
    """读取 Agent 角色：supervisor / subagent（默认 subagent）。"""
    if isinstance(agent, dict):
        cfg = agent.get("config") or {}
    else:
        cfg = agent.config or {}
    return str(cfg.get("role", "subagent")).lower()


def _agent_enabled(agent: AgentDefinition | dict[str, Any]) -> bool:
    """是否启用：enabled=false 时组装阶段跳过。"""
    if isinstance(agent, dict):
        cfg = agent.get("config") or {}
    else:
        cfg = agent.config or {}
    return bool(cfg.get("enabled", True))


def _chat_model_for_agent(
    db: Session,
    settings: Settings,
    agent: AgentDefinition | dict[str, Any],
) -> Any:
    """Supervisor / SubAgent 共用：快照 llm → model_id 目录 → Settings 默认。"""
    if isinstance(agent, dict):
        spec = resolve_model_spec_for_agent(
            db,
            model_id=agent.get("model_id"),
            snapshot_llm=agent.get("llm"),
        )
    else:
        spec = resolve_model_spec_for_agent(db, model_id=agent.model_id)
    return build_chat_model_from_spec(settings, spec)


def _resolve_runtime_bindings(
    db: Session,
    agent: AgentDefinition | dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], list[Any], list[Any], list[SkillDefinition]]:
    """
    把 live ORM 或 snapshot dict 归一为组装所需字段。

    Returns:
        (agent_id, name, system_prompt, config, tools, middleware, skills)
    """
    from deepagents_app.services.skills import load_skills_by_ids

    if isinstance(agent, dict):
        agent_id = str(agent.get("id") or agent["name"])
        name = str(agent["name"])
        system_prompt = agent.get("system_prompt") or ""
        config = dict(agent.get("config") or {})
        tools = load_tools_by_ids(db, list(agent.get("tool_ids") or []))
        middleware = load_middlewares_by_ids(db, list(agent.get("middleware_ids") or []))
        skills = load_skills_by_ids(db, list(agent.get("skill_ids") or []))
    else:
        agent_id = agent.id
        name = agent.name
        system_prompt = agent.system_prompt
        config = dict(agent.config or {})
        tools = []
        for t in agent.tools:
            tools.extend(expand_tool_definition(t))
        middleware = [load_middleware_object(m) for m in agent.middlewares]
        skills = list(agent.skills or [])
    return agent_id, name, system_prompt, config, tools, middleware, skills


def _build_subagent_spec(
    db: Session,
    settings: Settings,
    agent: AgentDefinition | dict[str, Any],
    *,
    scope: str,
) -> dict[str, Any]:
    """live / snapshot Agent → deepagents SubAgent 字典。"""
    from deepagents_app.services.skills import materialize_agent_skills

    agent_id, name, system_prompt, config, tools, middleware, skills = (
        _resolve_runtime_bindings(db, agent)
    )
    spec: dict[str, Any] = {
        "name": name,
        "description": str(config.get("description") or name),
        "system_prompt": system_prompt,
        "tools": tools,
        "model": _chat_model_for_agent(db, settings, agent),
    }
    skills_path = materialize_agent_skills(settings, agent_id, skills, scope=scope)
    if skills_path:
        spec["skills"] = [skills_path]
    if middleware:
        spec["middleware"] = middleware
    return spec


def _assemble_create_kwargs(
    *,
    settings: Settings,
    supervisor_prompt: str,
    supervisor_name: str,
    supervisor_model: Any,
    supervisor_tools: list[Any],
    supervisor_middleware: list[Any],
    supervisor_config: dict[str, Any],
    subagents: list[dict[str, Any]],
    supervisor_skills: list[str] | None = None,
) -> dict[str, Any]:
    """``create_deep_agent`` 参数组装（live / snapshot 共用）。"""
    interrupt_cfg = supervisor_config.get("interrupt_on")
    if interrupt_cfg is None:
        interrupt_on = build_interrupt_on(settings)
    else:
        interrupt_on = interrupt_cfg if settings.enable_hitl else None

    backend = build_filesystem_backend(settings)
    checkpointer = build_checkpointer(settings)
    permissions = build_permissions()
    configure_general_purpose_profile(settings)
    sync_memory_into_workspace(settings)
    memory_paths = (
        ["/AGENTS.md"] if (settings.workspace_dir / "AGENTS.md").exists() else None
    )

    create_kwargs: dict[str, Any] = {
        "model": supervisor_model,
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
    if supervisor_skills:
        create_kwargs["skills"] = list(supervisor_skills)
    return create_kwargs


def _split_roles(
    agents: list[AgentDefinition | dict[str, Any]],
    *,
    context: str,
) -> tuple[AgentDefinition | dict[str, Any], list[AgentDefinition | dict[str, Any]]]:
    """过滤 enabled，拆出 Supervisor 与 SubAgents。"""
    enabled = [a for a in agents if _agent_enabled(a)]
    supervisors = [a for a in enabled if _agent_role(a) == "supervisor"]
    subagents_defs = [a for a in enabled if _agent_role(a) != "supervisor"]
    if not supervisors:
        raise ValueError(f"{context} 缺少 Supervisor Agent")
    if len(supervisors) > 1:
        logger.warning("%s 有多个 Supervisor，使用第一个", context)
    return supervisors[0], subagents_defs


def _compile_from_agents(
    db: Session,
    settings: Settings,
    agents: list[AgentDefinition | dict[str, Any]],
    *,
    methodology_id: str,
    version: int,
    source: str,
) -> Any:
    """统一组装入口：agents 可为 live ORM 或 snapshot dict。"""
    from deepagents import create_deep_agent
    from deepagents_app.services.skills import (
        clear_materialized_skills,
        materialize_agent_skills,
    )

    scope = skills_scope(methodology_id, version)
    clear_materialized_skills(settings, scope=scope)

    context = f"方法论 {methodology_id}" if source == "live" else f"快照 {methodology_id} v{version}"
    supervisor, subagents_defs = _split_roles(agents, context=context)

    subagents = [
        _build_subagent_spec(db, settings, a, scope=scope) for a in subagents_defs
    ]
    (
        supervisor_id,
        supervisor_name,
        supervisor_prompt,
        supervisor_config,
        supervisor_tools,
        supervisor_middleware,
        supervisor_skills_rows,
    ) = _resolve_runtime_bindings(db, supervisor)
    supervisor_model = _chat_model_for_agent(db, settings, supervisor)
    skills_path = materialize_agent_skills(
        settings, supervisor_id, supervisor_skills_rows, scope=scope
    )
    supervisor_skills = [skills_path] if skills_path else None

    logger.info(
        "动态组装 Agent（%s）：methodology=%s v%s supervisor=%s subagents=%s",
        source,
        methodology_id,
        version,
        supervisor_name,
        [s["name"] for s in subagents],
    )

    kwargs = _assemble_create_kwargs(
        settings=settings,
        supervisor_prompt=supervisor_prompt,
        supervisor_name=supervisor_name,
        supervisor_model=supervisor_model,
        supervisor_tools=supervisor_tools,
        supervisor_middleware=supervisor_middleware,
        supervisor_config=supervisor_config,
        subagents=subagents,
        supervisor_skills=supervisor_skills,
    )
    return create_deep_agent(**kwargs)


def _build_from_live(
    db: Session,
    methodology: Methodology,
    settings: Settings,
) -> Any:
    """从当前 live 表组装 Agent。"""
    return _compile_from_agents(
        db,
        settings,
        list(methodology.agents),
        methodology_id=methodology.id,
        version=methodology.version,
        source="live",
    )


def _build_from_snapshot(
    db: Session,
    methodology_id: str,
    version: int,
    settings: Settings,
) -> Any:
    """从 MethodologyRevision 快照组装 Agent。"""
    revision = get_revision(db, methodology_id, version)
    if revision is None:
        raise LookupError(
            f"方法论快照不存在：{methodology_id} v{version}（无法按旧会话版本重建）"
        )
    snapshot = revision.snapshot or {}
    agents = list(snapshot.get("agents") or [])
    return _compile_from_agents(
        db,
        settings,
        agents,
        methodology_id=methodology_id,
        version=version,
        source="snapshot",
    )


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
    target_version = version if version is not None else methodology.version
    key = cache_key(methodology.id, target_version)

    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                logger.info("命中 Agent 缓存：%s", key)
                return cached

    if target_version == methodology.version:
        agent = _build_from_live(db, methodology, settings)
    else:
        agent = _build_from_snapshot(db, methodology.id, target_version, settings)

    if use_cache:
        with _cache_lock:
            _cache[key] = agent
    return agent
