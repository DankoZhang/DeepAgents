"""
Agent Factory（方法论驱动）
==========================

职责：按方法论（live 或历史快照）组装可运行的 deep agent，并做进程内缓存。

主流程::

    methodology_id (+ version)
      → 读 live 表 / MethodologyRevision 快照
      → 归一 Agent 规格（内嵌 tools / middlewares / skills / llm）
      → 绑定用户 workspace + 物化 Skills
      → create_deep_agent(...)
      → 按「用户 + 方法论 + 版本」LRU 缓存

缓存：
  - key = user_scope + methodology_id + version
  - 有上限 LRU；淘汰 / 失效时清理构建锁与对应 Skills 物化目录
  - 仅本进程有效，多 worker 各自一份

版本：
  - 旧会话按 Conversation.methodology_version 从快照重建
  - 与 live.version 一致时直接读当前表
  - live / snapshot 先归一成同形 dict，再走 ``_compile_from_agents``
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
from deepagents_app.registries.tools import (
    interrupt_tool_names_from_payloads,
    load_tools_from_snapshots,
)
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

# ── 进程内缓存（非跨 worker）──────────────────────────────────────────
# OrderedDict：命中 move_to_end，满则从头部 pop → LRU
_cache: OrderedDict[str, Any] = OrderedDict()
_cache_lock = threading.Lock()
# 同一 cache key 并发 miss 时只允许一个线程真正 create_deep_agent
_build_locks: dict[str, threading.Lock] = {}
_build_locks_guard = threading.Lock()


# ── 键与路径辅助 ──────────────────────────────────────────────────────


def cache_key(owner_user_id: str, methodology_id: str, version: int) -> str:
    """生成缓存键：用户 scope + 方法论 id + 版本号。"""
    return f"{user_scope_key(owner_user_id)}:{methodology_id}:v{version}"


def skills_scope(methodology_id: str, version: int) -> str:
    """
    Skills 物化目录隔离键。

    磁盘路径：``<用户工作区>/skills/<methodology_id>/v<version>/...``
    不同版本互不覆盖，便于旧会话按锁定版本重建。
    """
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
    """读取 LRU 上限；配置异常时回退 32。"""
    try:
        return int(get_settings().agent_cache_max_size)
    except Exception:  # noqa: BLE001
        return 32


# ── 缓存读写 / 失效 ──────────────────────────────────────────────────


def _drop_build_lock(key: str) -> None:
    """缓存条目消失后释放对应构建锁，避免 _build_locks 泄漏。"""
    with _build_locks_guard:
        _build_locks.pop(key, None)


def _cleanup_evicted_key(key: str, *, settings: Settings | None = None) -> None:
    """
    淘汰 / 失效某条缓存后的收尾。

    1. 去掉该 key 的进程内构建锁
    2. 尽量清空对应用户工作区里该方法论版本的 Skills 物化目录
    """
    _drop_build_lock(key)
    parsed = _parse_cache_key(key)
    if parsed is None:
        return
    uhash, methodology_id, version = parsed
    try:
        cfg = settings or get_settings()
        # 与 user_workspace_dir 布局一致：workspace/users/<uhash>/
        root = (cfg.workspace_dir / "users" / uhash).resolve()
        clear_materialized_skills(
            cfg,
            scope=skills_scope(methodology_id, version),
            workspace_root=root,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("清理物化 Skills 失败 key=%s: %s", key, exc)


def _build_lock_for(key: str) -> threading.Lock:
    """懒创建并返回某个 cache key 的构建锁（进程内线程互斥）。"""
    with _build_locks_guard:
        lock = _build_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _build_locks[key] = lock
        return lock


def _cache_get(key: str) -> Any | None:
    """命中则移到队尾（标记为最近使用）。"""
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
        # popitem(last=False)：弹出最久未使用的项
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
    - 都不指定：清空全部
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


# ── 配置加载与规格归一 ──────────────────────────────────────────────


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
    """
    live ORM → 与 snapshot 同形的 dict；dict 原样返回。

    统一后才能共用 ``_resolve_runtime_bindings``（要求内嵌 tools 等 payload）。
    """
    if isinstance(agent, dict):
        return agent
    return serialize_agent_for_snapshot(agent, include_llm=True)


def _agent_role(agent: dict[str, Any]) -> str:
    """从 config.role 读取角色；缺省视为 subagent。"""
    cfg = agent.get("config") or {}
    return str(cfg.get("role", "subagent")).lower()


def _agent_enabled(agent: dict[str, Any]) -> bool:
    """从 config.enabled 读取是否参与组装；缺省 True。"""
    cfg = agent.get("config") or {}
    return bool(cfg.get("enabled", True))


# ── 运行时绑定：模型 / 工具 / 中间件 / Skill ──────────────────────────


def _chat_model_for_agent(
    db: Session,
    settings: Settings,
    agent: dict[str, Any],
    *,
    owner_user_id: str,
) -> Any:
    """
    Supervisor / SubAgent 共用的模型解析。

    优先级：快照 llm（可补密钥）→ model_id 目录 → Settings/.env 默认。
    """
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

    要求快照 / 归一结果内嵌 tools / middlewares / skills 完整 payload，
    运行时直接展开，不再按 id 回查 live 目录表。
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

    # MCP 一条可展开为多个底层 tool；非 active 展开为空列表
    tools = load_tools_from_snapshots(list(agent.get("tools") or []))
    middleware = load_middlewares_from_snapshots(list(agent.get("middlewares") or []))
    # 仅保留 active Skill，供后续物化
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
        # description 供主 Agent 调度（task 工具列表展示）
        "description": str(config.get("description") or name),
        "system_prompt": system_prompt,
        "tools": tools,
        "model": _chat_model_for_agent(
            db, settings, agent, owner_user_id=owner_user_id
        ),
    }
    # Skill 需落盘成 SKILL.md；返回虚拟路径再塞进 create_deep_agent(skills=...)
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
    catalog_interrupt_on: dict[str, bool] | None = None,
    supervisor_skills: list[str] | None = None,
) -> dict[str, Any]:
    """``create_deep_agent`` 参数组装（live / snapshot 共用）。"""
    interrupt_on = _resolve_interrupt_on(
        settings,
        supervisor_config=supervisor_config,
        catalog_interrupt_on=catalog_interrupt_on,
    )

    memory_file = workspace_root / "AGENTS.md"
    create_kwargs: dict[str, Any] = {
        "model": supervisor_model,
        "system_prompt": supervisor_prompt,
        "subagents": subagents,
        # 虚拟 FS 根钉死到用户工作区；Memory / Skills / 摘要落盘都走它
        "backend": build_filesystem_backend(
            settings, workspace_root=workspace_root
        ),
        # 用户自定义中间件；框架默认栈（TodoList/FS/Summarization 等）由 create_deep_agent 自动挂
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


def _resolve_interrupt_on(
    settings: Settings,
    *,
    supervisor_config: dict[str, Any],
    catalog_interrupt_on: dict[str, bool] | None,
) -> dict[str, bool] | None:
    """
    HITL 名单 = 系统默认（框架原生）∪ 目录工具 requires_hitl。

    Supervisor ``config.interrupt_on`` 若存在则再合并覆盖；总闸 ``enable_hitl``。
    """
    if not settings.enable_hitl:
        return None
    merged: dict[str, bool] = {}
    system = build_interrupt_on(settings)
    if system:
        merged.update(system)
    if catalog_interrupt_on:
        merged.update(catalog_interrupt_on)
    explicit = supervisor_config.get("interrupt_on")
    if isinstance(explicit, dict):
        merged.update({str(k): bool(v) for k, v in explicit.items()})
    return merged or None


def _catalog_interrupt_on_from_agents(
    agents: list[dict[str, Any]],
) -> dict[str, bool]:
    """汇总方法论内所有 Agent 绑定工具的 requires_hitl 运行时名。"""
    merged: dict[str, bool] = {}
    for agent in agents:
        payloads = list(agent.get("tools") or [])
        merged.update(interrupt_tool_names_from_payloads(payloads))
    return merged


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


# ── 核心组装 ──────────────────────────────────────────────────────────


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
    """
    统一组装入口：live ORM / snapshot dict 在此归一后走同一条路径。

    仅在缓存未命中、需要真正 ``create_deep_agent`` 时调用（非每条聊天）。
    """
    from deepagents import create_deep_agent

    # live ORM 与 snapshot dict → 同形规格，后续路径完全一致
    specs = [_normalize_agent_spec(a) for a in agents]
    scope = skills_scope(methodology_id, version)
    # 确保用户工作区存在（含 documents/notes/audit/skills、同步 AGENTS.md）
    workspace_root = user_workspace_dir(settings, owner_user_id)

    context = (
        f"方法论 {methodology_id}"
        if source == "live"
        else f"快照 {methodology_id} v{version}"
    )

    # 请求上下文：ContextVar 绑定当前用户工作区根（不是锁）
    # 工具 / Audit 等通过 get_workspace_root() 落到该目录，请求结束自动 reset
    with workspace_context(workspace_root):
        # 跨进程文件锁：锁的是「该用户下某方法论版本的 Skills 物化 scope」
        # 防止多 worker 同时 clear/write 同一目录互相踩踏（不是锁用户、也不是锁请求）
        with interprocess_lock(skills_materialize_lock(workspace_root, scope)):
            # 先清空整个 scope（含其下所有 agent_id），再按 Agent 重新物化
            clear_materialized_skills(
                settings, scope=scope, workspace_root=workspace_root
            )

            supervisor, subagents_defs = _split_roles(specs, context=context)

            # 子 Agent：各自展开工具/中间件，并把 Skills 物化到
            # <workspace>/skills/<scope>/<agent_id>/<name>/SKILL.md
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
                catalog_interrupt_on=_catalog_interrupt_on_from_agents(specs),
                supervisor_skills=supervisor_skills,
            )
            return create_deep_agent(**kwargs)


def _build_from_live(
    db: Session,
    methodology: Methodology,
    settings: Settings,
) -> Any:
    """从当前 live 表组装 Agent（会话版本 == 方法论当前 version）。"""
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
    """从 MethodologyRevision 快照组装 Agent（旧会话锁定的历史版本）。"""
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


# ── 对外主入口 ────────────────────────────────────────────────────────


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
    - 同 key 命中进程缓存则直接返回，避免重复 ``create_deep_agent`` / 重复物化 Skills

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

    # 按 key 加锁：缓存 miss 时同进程并发请求只组装一次
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
