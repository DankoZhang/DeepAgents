"""
Agent Factory（方法论驱动）
==========================

流程：
  methodology_id → 加载 Agent/SubAgent/Tool/Middleware → create_deep_agent()

缓存：
  key = user_scope + methodology_id + version
    有上限 LRU；淘汰时清理构建锁与对应用户 workspace 下的 Skills 物化目录

版本：
  旧会话按 Conversation.methodology_version 从快照重建；
  与当前行一致时直接读 live 表。
  live / snapshot 先归一成统一 Agent 规格，再走同一条组装路径。
"""

from __future__ import annotations

import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.backends import build_filesystem_backend
from deepagents_app.config import Settings, get_settings
from deepagents_app.db.loading import methodology_with_agents_options
from deepagents_app.db.models import AgentDefinition, Methodology, SkillDefinition
from deepagents_app.factory import (
    build_checkpointer,
    build_general_purpose_subagent,
    build_interrupt_on,
    build_permissions,
)
from deepagents_app.llm import build_chat_model_from_spec
from deepagents_app.ownership import user_scope_key
from deepagents_app.registries.middleware import load_middlewares_from_snapshots
from deepagents_app.registries.tools import load_tools_from_snapshots
from deepagents_app.services.llm_models import resolve_model_spec_for_agent
from deepagents_app.services.revisions import get_revision, serialize_agent_for_snapshot
from deepagents_app.services.skills import (
    clear_materialized_skills,
    load_skills_from_snapshots,
    materialize_agent_skills,
)
from deepagents_app.workspace import (
    interprocess_lock,
    skills_materialize_lock,
    user_workspace_dir,
    workspace_context,
)

logger = logging.getLogger(__name__)

# 进程内 Compiled Agent 缓存（OrderedDict 充当 LRU）
_cache: OrderedDict[str, Any] = OrderedDict()
_cache_lock = threading.Lock()
# 按 cache key 串行化构建，避免并发 miss 时重复 create_deep_agent
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


def cache_key(owner_user_id: str, methodology_id: str, version: int) -> str:
    """生成缓存键：用户 scope + 方法论 id + 版本号。"""
    return f"{user_scope_key(owner_user_id)}:{methodology_id}:v{version}"


def skills_scope(methodology_id: str, version: int) -> str:
    """Skills 物化目录隔离键（不同版本写不同路径，避免互相覆盖）。"""
    return f"{methodology_id}/v{version}"


def _parse_cache_key(key: str) -> tuple[str, str, int] | None:
    """``{uhash}:{methodology_id}:v{version}`` → 元组；无法解析返回 None。"""
    left, sep, ver = key.rpartition(":v")
    if not sep or not left:
        return None
    uhash, sep2, mid = left.partition(":")
    if not sep2 or not uhash or not mid:
        return None
    try:
        return uhash, mid, int(ver)
    except ValueError:
        return None


def _cache_max_size() -> int:
    try:
        return int(get_settings().agent_cache_max_size)
    except Exception:  # noqa: BLE001
        return 32


def _drop_build_lock(key: str) -> None:
    with _build_locks_guard:
        _build_locks.pop(key, None)


def _cleanup_evicted_key(key: str, *, settings: Settings | None = None) -> None:
    """淘汰 / 失效某条缓存后：释放构建锁，并尽量清掉对应用户工作区物化目录。"""
    _drop_build_lock(key)
    parsed = _parse_cache_key(key)
    if parsed is None:
        return
    uhash, methodology_id, version = parsed
    try:
        cfg = settings or get_settings()
        root = (cfg.workspace_dir / "users" / uhash).resolve()
        clear_materialized_skills(
            cfg,
            scope=skills_scope(methodology_id, version),
            workspace_root=root,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理物化 Skills 失败 key=%s: %s", key, exc)


def _build_lock_for(key: str) -> threading.Lock:
    """懒创建并返回某个 cache key 的构建锁。"""
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


def _cache_get(key: str) -> Any | None:
    with _cache_lock:
        if key not in _cache:
            return None
        _cache.move_to_end(key)
        return _cache[key]


def _cache_put(key: str, value: Any) -> list[str]:
    """写入缓存；若超额则按 LRU 淘汰，返回被淘汰的 key 列表。"""
    maxsize = _cache_max_size()
    evicted: list[str] = []
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            _cache[key] = value
            return evicted
        while len(_cache) >= maxsize:
            old_key, _ = _cache.popitem(last=False)
            evicted.append(old_key)
        _cache[key] = value
    return evicted


def invalidate_agent_cache(
    methodology_id: str | None = None,
    version: int | None = None,
    *,
    owner_user_id: str | None = None,
) -> int:
    """
    失效缓存，并清理对应构建锁 / 物化目录。

    - 指定 id+version：删匹配条目（可带 owner 精确定位）
    - 仅指定 id：删该方法论所有版本
    - 都不指定：清空
    """
    with _cache_lock:
        if methodology_id is None:
            keys = list(_cache.keys())
            _cache.clear()
        elif version is not None:
            if owner_user_id is not None:
                key = cache_key(owner_user_id, methodology_id, version)
                _cache.pop(key, None)
                keys = [key]
            else:
                suffix = f":{methodology_id}:v{version}"
                keys = [k for k in list(_cache) if k.endswith(suffix)]
                for k in keys:
                    del _cache[k]
        else:
            needle = f":{methodology_id}:v"
            keys = [k for k in list(_cache) if needle in k]
            for k in keys:
                del _cache[k]

    for key in keys:
        _cleanup_evicted_key(key)
    return len(keys)


def get_methodology_config(
    db: Session,
    methodology_id: str,
    *,
    owner_user_id: str | None = None,
) -> Methodology:
    """查询方法论 live 行（含 agents / tools / middlewares / skills / llm）。"""
    q = (
        db.query(Methodology)
        .options(*methodology_with_agents_options())
        .filter(Methodology.id == methodology_id)
    )
    if owner_user_id is not None:
        q = q.filter(Methodology.owner_user_id == owner_user_id)
    methodology = q.one_or_none()
    if methodology is None:
        raise NotFoundError(f"方法论不存在：{methodology_id}")
    return methodology


def _normalize_agent_spec(agent: AgentDefinition | dict[str, Any]) -> dict[str, Any]:
    """live ORM → 与 snapshot 同形的 dict；dict 原样返回。"""
    if isinstance(agent, dict):
        return agent
    return serialize_agent_for_snapshot(agent, include_llm=True)


def _agent_role(agent: dict[str, Any]) -> str:
    cfg = agent.get("config") or {}
    return str(cfg.get("role", "subagent")).lower()


def _agent_enabled(agent: dict[str, Any]) -> bool:
    cfg = agent.get("config") or {}
    return bool(cfg.get("enabled", True))


def _chat_model_for_agent(
    db: Session,
    settings: Settings,
    agent: dict[str, Any],
    *,
    owner_user_id: str,
) -> Any:
    """Supervisor / SubAgent 共用：快照 llm → model_id 目录 → Settings 默认。"""
    spec = resolve_model_spec_for_agent(
        db,
        owner_user_id=owner_user_id,
        model_id=agent.get("model_id"),
        snapshot_llm=agent.get("llm"),
    )
    return build_chat_model_from_spec(settings, spec)


def _resolve_runtime_bindings(
    agent: dict[str, Any],
) -> tuple[str, str, str, dict[str, Any], list[Any], list[Any], list[SkillDefinition]]:
    """
    从归一后的 Agent dict 解析组装字段。

    要求快照 / 归一结果内嵌 tools / middlewares / skills payload。
    """
    agent_id = str(agent.get("id") or agent["name"])
    name = str(agent["name"])
    system_prompt = agent.get("system_prompt") or ""
    config = dict(agent.get("config") or {})

    if "tools" not in agent:
        raise BusinessError(f"Agent {agent_id} 缺少 tools payload，无法组装")
    if "middlewares" not in agent:
        raise BusinessError(f"Agent {agent_id} 缺少 middlewares payload，无法组装")
    if "skills" not in agent:
        raise BusinessError(f"Agent {agent_id} 缺少 skills payload，无法组装")

    tools = load_tools_from_snapshots(list(agent.get("tools") or []))
    middleware = load_middlewares_from_snapshots(list(agent.get("middlewares") or []))
    skills = load_skills_from_snapshots(list(agent.get("skills") or []))
    return agent_id, name, system_prompt, config, tools, middleware, skills


def _build_subagent_spec(
    db: Session,
    settings: Settings,
    agent: dict[str, Any],
    *,
    scope: str,
    owner_user_id: str,
    workspace_root: Path,
) -> dict[str, Any]:
    """归一后的 Agent dict → deepagents SubAgent 字典。"""
    agent_id, name, system_prompt, config, tools, middleware, skills = (
        _resolve_runtime_bindings(agent)
    )
    spec: dict[str, Any] = {
        "name": name,
        "description": str(config.get("description") or name),
        "system_prompt": system_prompt,
        "tools": tools,
        "model": _chat_model_for_agent(
            db, settings, agent, owner_user_id=owner_user_id
        ),
    }
    # Skill 需落盘成 SKILL.md；返回的虚拟路径再塞进 create_deep_agent
    skills_path = materialize_agent_skills(
        settings,
        agent_id,
        skills,
        scope=scope,
        workspace_root=workspace_root,
    )
    if skills_path:
        spec["skills"] = [skills_path]
    if middleware:
        spec["middleware"] = middleware
    return spec


def _assemble_create_kwargs(
    *,
    settings: Settings,
    workspace_root: Path,
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
    # HITL：未显式配置时跟全局开关；显式配置了也要受 enable_hitl 总闸控制
    interrupt_cfg = supervisor_config.get("interrupt_on")
    if interrupt_cfg is None:
        interrupt_on = build_interrupt_on(settings)
    else:
        interrupt_on = interrupt_cfg if settings.enable_hitl else None

    memory_file = workspace_root / "AGENTS.md"
    create_kwargs: dict[str, Any] = {
        "model": supervisor_model,
        "system_prompt": supervisor_prompt,
        "subagents": subagents,
        "backend": build_filesystem_backend(
            settings, workspace_root=workspace_root
        ),
        "middleware": supervisor_middleware,
        # 用户工作区根下有 AGENTS.md 才注入 memory，避免空路径报错
        "memory": ["/AGENTS.md"] if memory_file.exists() else None,
        "permissions": build_permissions(),
        "interrupt_on": interrupt_on,
        # Redis/内存 checkpointer：多轮对话与 HITL resume 都依赖它
        "checkpointer": build_checkpointer(settings),
        "name": supervisor_name,
    }
    if supervisor_tools:
        create_kwargs["tools"] = supervisor_tools
    if supervisor_skills:
        create_kwargs["skills"] = list(supervisor_skills)
    return create_kwargs


def _split_roles(
    agents: list[dict[str, Any]],
    *,
    context: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """过滤 enabled，拆出 Supervisor 与 SubAgents；无 Supervisor 则无法组装。"""
    enabled = [a for a in agents if _agent_enabled(a)]
    supervisors = [a for a in enabled if _agent_role(a) == "supervisor"]
    subagents_defs = [a for a in enabled if _agent_role(a) != "supervisor"]
    if not supervisors:
        raise BusinessError(f"{context} 缺少 Supervisor Agent")
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
    owner_user_id: str,
) -> Any:
    """统一组装入口：入口处将 live ORM / snapshot dict 归一为 dict。"""
    from deepagents import create_deep_agent

    # live ORM 与 snapshot dict 在此统一成同形规格，后续路径完全一致
    specs = [_normalize_agent_spec(a) for a in agents]
    scope = skills_scope(methodology_id, version)
    workspace_root = user_workspace_dir(settings, owner_user_id)

    context = (
        f"方法论 {methodology_id}"
        if source == "live"
        else f"快照 {methodology_id} v{version}"
    )

    with workspace_context(workspace_root):
        # 跨进程锁：多 worker 同时 clear/write 同一 scope 时串行化
        with interprocess_lock(skills_materialize_lock(workspace_root, scope)):
            clear_materialized_skills(
                settings, scope=scope, workspace_root=workspace_root
            )

            supervisor, subagents_defs = _split_roles(specs, context=context)

            subagents = [
                _build_subagent_spec(
                    db,
                    settings,
                    a,
                    scope=scope,
                    owner_user_id=owner_user_id,
                    workspace_root=workspace_root,
                )
                for a in subagents_defs
            ]
            (
                supervisor_id,
                supervisor_name,
                supervisor_prompt,
                supervisor_config,
                supervisor_tools,
                supervisor_middleware,
                supervisor_skills_rows,
            ) = _resolve_runtime_bindings(supervisor)
            supervisor_model = _chat_model_for_agent(
                db, settings, supervisor, owner_user_id=owner_user_id
            )
            skills_path = materialize_agent_skills(
                settings,
                supervisor_id,
                supervisor_skills_rows,
                scope=scope,
                workspace_root=workspace_root,
            )
            supervisor_skills = [skills_path] if skills_path else None

            # 显式注入 general-purpose，避免依赖全局 HarnessProfile
            if not any(s.get("name") == "general-purpose" for s in subagents):
                subagents.append(
                    build_general_purpose_subagent(
                        model=supervisor_model,
                        specialist_names=[str(s["name"]) for s in subagents],
                    )
                )

            logger.info(
                "动态组装 Agent（%s）：methodology=%s v%s user_ws=%s "
                "supervisor=%s subagents=%s",
                source,
                methodology_id,
                version,
                workspace_root,
                supervisor_name,
                [s["name"] for s in subagents],
            )

            kwargs = _assemble_create_kwargs(
                settings=settings,
                workspace_root=workspace_root,
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
        owner_user_id=methodology.owner_user_id,
    )


def _build_from_snapshot(
    db: Session,
    methodology_id: str,
    version: int,
    settings: Settings,
    *,
    owner_user_id: str,
) -> Any:
    """从 MethodologyRevision 快照组装 Agent。"""
    revision = get_revision(db, methodology_id, version)
    if revision is None:
        raise NotFoundError(
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
        owner_user_id=owner_user_id,
    )


def build_agent_from_methodology(
    db: Session,
    methodology_id: str,
    *,
    owner_user_id: str | None = None,
    version: int | None = None,
    settings: Settings | None = None,
    use_cache: bool = True,
) -> Any:
    """
    根据方法论动态创建 Compiled Agent（对外主入口）。

    - ``version is None`` 或等于 live.version → 读当前表组装
    - ``version`` 落后于 live → 从 ``MethodologyRevision`` 快照重建（旧会话）
    - 同 key 命中进程缓存则直接返回，避免重复 ``create_deep_agent``

    Returns:
        LangGraph CompiledStateGraph
    """
    settings = settings or get_settings()
    methodology = get_methodology_config(
        db,
        methodology_id,
        owner_user_id=owner_user_id,
    )
    owner = owner_user_id or methodology.owner_user_id
    target_version = version if version is not None else methodology.version
    key = cache_key(owner, methodology.id, target_version)

    # 按 key 加锁：缓存 miss 时并发请求只组装一次
    with _build_lock_for(key):
        if use_cache:
            cached = _cache_get(key)
            if cached is not None:
                logger.info("命中 Agent 缓存：%s", key)
                return cached

        # 版本与 live 一致走当前配置；否则按会话锁定的旧 version 从快照重建
        if target_version == methodology.version:
            agent = _build_from_live(db, methodology, settings)
        else:
            agent = _build_from_snapshot(
                db,
                methodology.id,
                target_version,
                settings,
                owner_user_id=owner,
            )

        if use_cache:
            for evicted in _cache_put(key, agent):
                logger.info("Agent 缓存 LRU 淘汰：%s", evicted)
                _cleanup_evicted_key(evicted, settings=settings)
        return agent
