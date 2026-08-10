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
  - 有上限 LRU；淘汰 / 失效时清理构建锁
  - Skills 按内容指纹物化（只写不删），与缓存生命周期解耦
  - 进程内 LRU；失效经 Redis pub/sub 广播到其他 worker

版本：
  - 旧会话按 Conversation.methodology_version 从快照重建
  - 与 live.version 一致时直接读当前表
  - live / snapshot 先归一成同形 dict，再走 ``_compile_from_agents``
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import OrderedDict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
from deepagents_app.services.catalog.llm_models import resolve_model_spec_for_agent
from deepagents_app.services.versioning.memory import (
    materialize_versioned_memory,
    read_project_memory,
)
from deepagents_app.services.versioning.revisions import get_revision
from deepagents_app.services.versioning.content_blobs import hydrate_snapshot_content
from deepagents_app.services.versioning.snapshots import serialize_agent_for_live
from deepagents_app.services.catalog.skills import (
    load_skills_from_snapshots,
    materialize_agent_skills,
    materialized_skills_dir_from_virtual,
    touch_materialized_skills_complete,
)
from deepagents_app.workspace import (
    user_workspace_dir,
    workspace_context,
)

logger = logging.getLogger(__name__)

# ── 进程内缓存（跨 worker 靠 Redis pub/sub 失效）──────────────────────
# OrderedDict：命中 move_to_end，满则从头部 pop → LRU
_cache: OrderedDict[str, Any] = OrderedDict()
# 与 _cache 同 key：该编译体依赖的 Skills 物化目录（供命中时 touch .complete）
_cache_skill_roots: dict[str, tuple[Path, ...]] = {}
_cache_lock = threading.Lock()
# 同一 cache key 并发 miss 时只组装一次（必须用 asyncio.Lock，不可 threading.Lock 包 await）
_build_locks: dict[str, asyncio.Lock] = {}


# ── 键与路径辅助 ──────────────────────────────────────────────────────


def cache_key(owner_user_id: str, methodology_id: str, version: int) -> str:
    """生成缓存键：用户 scope + 方法论 id + 版本号。"""
    return f"{user_scope_key(owner_user_id)}:{methodology_id}:v{version}"


def _cache_max_size() -> int:
    """读取 LRU 上限；配置异常时回退 32。"""
    try:
        return int(get_settings().agent_cache_max_size)
    except Exception:  # noqa: BLE001
        return 32


# ── 缓存读写 / 失效 ──────────────────────────────────────────────────


def _drop_build_lock(key: str) -> None:
    """
    缓存条目消失后尝试释放构建锁。

    若锁仍被持有（正在组装），保留条目，避免并发 miss 各自新建锁、重复编译。
    """
    lock = _build_locks.get(key)
    if lock is None:
        return
    if lock.locked():
        return
    _build_locks.pop(key, None)


async def _cleanup_failed_build_lock(key: str) -> None:
    """失败建构释放无主锁；先让已唤醒的等待者重新取得锁。"""
    # asyncio.Lock.release() 唤醒 waiter 后，waiter 会在下一轮事件循环中才将
    # 锁标记为 held。让出一次执行权，避免删掉仍有 waiter 的锁字典条目。
    await asyncio.sleep(0)
    _drop_build_lock(key)


def _build_lock_for(key: str) -> asyncio.Lock:
    """懒创建并返回某个 cache key 的 asyncio 构建锁。"""
    lock = _build_locks.get(key)
    if lock is None:
        # 无 await 的 dict 读写在单线程事件循环内安全
        lock = asyncio.Lock()
        _build_locks[key] = lock
    return lock


def _cache_get(key: str) -> Any | None:
    """命中则移到队尾（标记为最近使用）。"""
    with _cache_lock:
        if key not in _cache:
            return None
        _cache.move_to_end(key)
        return _cache[key]


def _cache_skill_roots_for(key: str) -> tuple[Path, ...]:
    with _cache_lock:
        return _cache_skill_roots.get(key, ())


async def _cache_hit(key: str) -> Any | None:
    """
    命中缓存则返回编译体，并刷新关联 Skills ``.complete`` mtime。

    长期 LRU 命中若不 touch，GC 会按过期 mtime 删掉仍在用的物化目录。
    """
    cached = _cache_get(key)
    if cached is None:
        return None
    roots = _cache_skill_roots_for(key)
    if roots:
        await asyncio.to_thread(touch_materialized_skills_complete, roots)
    logger.info("命中 Agent 缓存：%s", key)
    return cached


def _cache_put(
    key: str,
    value: Any,
    *,
    skill_roots: Sequence[Path] | None = None,
) -> list[str]:
    """写入缓存；若超额则按 LRU 淘汰，返回被淘汰的 key 列表。"""
    maxsize = _cache_max_size()
    evicted: list[str] = []
    roots = tuple(skill_roots or ())
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            _cache[key] = value
            _cache_skill_roots[key] = roots
            return evicted
        # popitem(last=False)：弹出最久未使用的项
        while len(_cache) >= maxsize:
            old_key, _ = _cache.popitem(last=False)
            _cache_skill_roots.pop(old_key, None)
            evicted.append(old_key)
        _cache[key] = value
        _cache_skill_roots[key] = roots
    return evicted


def invalidate_agent_cache_local(
    methodology_id: str | None = None,
    version: int | None = None,
    *,
    owner_user_id: str | None = None,
) -> int:
    """
    仅失效本进程 Agent 编译缓存（供 Redis 订阅回调；不广播）。

    Skills 物化目录按内容寻址保留，不随缓存失效删除。

    - 指定 id+version：删匹配条目（可带 owner 精确定位）
    - 仅指定 id：删该方法论所有版本
    - 都不指定：清空全部
    """
    with _cache_lock:
        if methodology_id is None:
            keys = list(_cache.keys())
            _cache.clear()
            _cache_skill_roots.clear()
        elif version is not None:
            if owner_user_id is not None:
                key = cache_key(owner_user_id, methodology_id, version)
                _cache.pop(key, None)
                _cache_skill_roots.pop(key, None)
                keys = [key]
            else:
                suffix = f":{methodology_id}:v{version}"
                keys = [k for k in list(_cache) if k.endswith(suffix)]
                for k in keys:
                    del _cache[k]
                    _cache_skill_roots.pop(k, None)
        else:
            needle = f":{methodology_id}:v"
            keys = [k for k in list(_cache) if needle in k]
            for k in keys:
                del _cache[k]
                _cache_skill_roots.pop(k, None)

    for key in keys:
        _drop_build_lock(key)
    return len(keys)


def invalidate_agent_cache(
    methodology_id: str | None = None,
    version: int | None = None,
    *,
    owner_user_id: str | None = None,
) -> int:
    """本进程失效后，经 Redis pub/sub 通知其他 worker。"""
    removed = invalidate_agent_cache_local(
        methodology_id,
        version,
        owner_user_id=owner_user_id,
    )
    try:
        from deepagents_app.services.infra.cache_pubsub import publish_cache_invalidation

        publish_cache_invalidation(
            methodology_id=methodology_id,
            version=version,
            owner_user_id=owner_user_id,
            all_keys=methodology_id is None,
        )
    except Exception:  # noqa: BLE001
        logger.debug("广播 Agent 缓存失效失败", exc_info=True)
    return removed


# ── 配置加载与规格归一 ──────────────────────────────────────────────


async def get_methodology_config(
    db: AsyncSession,
    methodology_id: str,
    *,
    owner_user_id: str | None = None,
) -> Methodology:
    """查询方法论 live 行（含 agents / tools / middlewares / skills / llm）。"""
    stmt = (
        select(Methodology)
        .options(*methodology_with_agents_options())
        .where(Methodology.id == methodology_id)
    )
    if owner_user_id is not None:
        stmt = stmt.where(Methodology.owner_user_id == owner_user_id)
    methodology = (await db.scalars(stmt)).one_or_none()
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
    return serialize_agent_for_live(agent)


def _agent_role(agent: dict[str, Any]) -> str:
    """从 config.role 读取角色；缺省视为 subagent。"""
    cfg = agent.get("config") or {}
    return str(cfg.get("role", "subagent")).lower()


def _agent_enabled(agent: dict[str, Any]) -> bool:
    """从 config.enabled 读取是否参与组装；缺省 True。"""
    cfg = agent.get("config") or {}
    return bool(cfg.get("enabled", True))


# ── 运行时绑定：模型 / 工具 / 中间件 / Skill ──────────────────────────


async def _chat_model_for_agent(
    db: AsyncSession,
    settings: Settings,
    agent: dict[str, Any],
    *,
    owner_user_id: str,
) -> Any:
    """
    Supervisor / SubAgent 共用的模型解析。

    优先级：快照 llm（可补密钥）→ model_id 目录 → Settings/.env 默认。
    """
    spec = await resolve_model_spec_for_agent(
        db,
        owner_user_id=owner_user_id,
        model_id=agent.get("model_id"),
        snapshot_llm=agent.get("llm"),
    )
    return build_chat_model_from_spec(settings, spec)


async def _resolve_runtime_bindings(
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
    tools = await load_tools_from_snapshots(list(agent.get("tools") or []))
    middleware = load_middlewares_from_snapshots(list(agent.get("middlewares") or []))
    # 仅保留 active Skill，供后续物化
    skills = load_skills_from_snapshots(list(agent.get("skills") or []))
    return agent_id, name, system_prompt, config, tools, middleware, skills


async def _build_subagent_spec(
    db: AsyncSession,
    settings: Settings,
    agent: dict[str, Any],
    *,
    owner_user_id: str,
    workspace_root: Path,
) -> dict[str, Any]:
    """归一后的 Agent dict → deepagents SubAgent 字典。"""
    agent_id, name, system_prompt, config, tools, middleware, skills = (
        await _resolve_runtime_bindings(agent)
    )
    spec: dict[str, Any] = {
        "name": name,
        # description 供主 Agent 调度（task 工具列表展示）
        "description": str(config.get("description") or name),
        "system_prompt": system_prompt,
        "tools": tools,
        "model": await _chat_model_for_agent(
            db, settings, agent, owner_user_id=owner_user_id
        ),
    }
    # Skill 按内容指纹落盘；返回虚拟路径再塞进 create_deep_agent(skills=...)
    # 同步磁盘 I/O 丢到线程池，避免卡住事件循环
    skills_path = await asyncio.to_thread(
        materialize_agent_skills,
        settings,
        agent_id,
        skills,
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
    memory_paths: list[str] | None = None,
) -> dict[str, Any]:
    """``create_deep_agent`` 参数组装（live / snapshot 共用）。"""
    interrupt_on = _resolve_interrupt_on(
        settings,
        supervisor_config=supervisor_config,
        catalog_interrupt_on=catalog_interrupt_on,
    )

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
        # 版本化物化路径；无 Memory 时不注入
        "memory": list(memory_paths) if memory_paths else None,
        "permissions": build_permissions(),
        "interrupt_on": interrupt_on,
        # Redis checkpointer：多轮对话与 HITL resume 都依赖它
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
    HITL 名单 = 系统默认（框架原生，受 ``enable_hitl`` 总闸）∪
    目录工具 ``requires_hitl``（不受总闸影响）∪ Supervisor 显式配置。
    """
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


async def _catalog_interrupt_on_from_agents(
    agents: list[dict[str, Any]],
) -> dict[str, bool]:
    """汇总方法论内所有 Agent 绑定工具的 requires_hitl 运行时名。"""
    merged: dict[str, bool] = {}
    for agent in agents:
        payloads = list(agent.get("tools") or [])
        merged.update(await interrupt_tool_names_from_payloads(payloads))
    return merged


def _split_roles(
    agents: list[dict[str, Any]],
    *,
    context: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """过滤 enabled，拆出 Supervisor 与 SubAgents（与 publish 共用 roles 口径）。"""
    from deepagents_app.services.catalog.roles import require_single_supervisor

    return require_single_supervisor(
        agents,
        context=context,
        role_of=_agent_role,
        name_of=lambda a: str(a.get("name") or a.get("id") or "?"),
        enabled_of=_agent_enabled,
    )


# ── 核心组装 ──────────────────────────────────────────────────────────


async def _compile_from_agents(
    db: AsyncSession,
    settings: Settings,
    agents: list[AgentDefinition | dict[str, Any]],
    *,
    methodology_id: str,
    version: int,
    source: str,
    owner_user_id: str,
    memory_content: str | None = None,
) -> tuple[Any, list[Path]]:
    """
    统一组装入口：live ORM / snapshot dict 在此归一后走同一条路径。

    仅在缓存未命中、需要真正 ``create_deep_agent`` 时调用（非每条聊天）。
    返回 ``(compiled_agent, skills_materialize_roots)``，供 LRU 命中时续租约。
    """
    from deepagents import create_deep_agent

    # live ORM 与 snapshot dict → 同形规格，后续路径完全一致
    specs = [_normalize_agent_spec(a) for a in agents]
    # 确保用户工作区存在（含 documents/notes/audit/skills）；mkdir 走线程池
    workspace_root = await asyncio.to_thread(
        user_workspace_dir, settings, owner_user_id
    )

    context = (
        f"方法论 {methodology_id}"
        if source == "live"
        else f"快照 {methodology_id} v{version}"
    )

    # 请求上下文：ContextVar 绑定当前用户工作区根
    # 工具 / Audit 等通过 get_workspace_root() 落到该目录，请求结束自动 reset
    with workspace_context(workspace_root):
        supervisor, subagents_defs = _split_roles(specs, context=context)

        # 子 Agent：各自展开工具/中间件；Skills 按内容指纹物化（只写不删）
        # <workspace>/skills/<fingerprint>/<agent_id>/<name>/SKILL.md
        subagents = [
            await _build_subagent_spec(
                db,
                settings,
                a,
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
        ) = await _resolve_runtime_bindings(supervisor)
        supervisor_model = await _chat_model_for_agent(
            db, settings, supervisor, owner_user_id=owner_user_id
        )
        skills_path = await asyncio.to_thread(
            materialize_agent_skills,
            settings,
            supervisor_id,
            supervisor_skills_rows,
            workspace_root=workspace_root,
        )
        supervisor_skills = [skills_path] if skills_path else None

        # Memory：快照正文优先；live 读项目级文件；按方法论版本物化
        # 磁盘读写 + create_deep_agent 放到线程池，锁内也不堵事件循环
        catalog_interrupt_on = await _catalog_interrupt_on_from_agents(specs)

        def _materialize_and_create() -> Any:
            resolved_memory = (
                memory_content
                if memory_content is not None
                else read_project_memory(settings)
            )
            memory_virtual = materialize_versioned_memory(
                workspace_root,
                methodology_id=methodology_id,
                version=version,
                content=resolved_memory,
            )
            memory_paths = [memory_virtual] if memory_virtual else None

            # 显式注入 general-purpose，避免依赖全局 HarnessProfile
            final_subagents = list(subagents)
            if not any(s.get("name") == "general-purpose" for s in final_subagents):
                final_subagents.append(
                    build_general_purpose_subagent(
                        model=supervisor_model,
                        specialist_names=[str(s["name"]) for s in final_subagents],
                    )
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
                subagents=final_subagents,
                catalog_interrupt_on=catalog_interrupt_on,
                supervisor_skills=supervisor_skills,
                memory_paths=memory_paths,
            )
            return create_deep_agent(**kwargs), memory_virtual

        agent, memory_virtual = await asyncio.to_thread(_materialize_and_create)
        skill_virtuals: list[str] = []
        if skills_path:
            skill_virtuals.append(skills_path)
        for spec in subagents:
            for vp in spec.get("skills") or []:
                if isinstance(vp, str) and vp:
                    skill_virtuals.append(vp)
        skill_roots: list[Path] = []
        for vp in skill_virtuals:
            root = materialized_skills_dir_from_virtual(workspace_root, vp)
            if root is not None:
                skill_roots.append(root)
        logger.info(
            "动态组装 Agent（%s）：methodology=%s v%s user_ws=%s "
            "supervisor=%s subagents=%s memory=%s",
            source,
            methodology_id,
            version,
            workspace_root,
            supervisor_name,
            [s["name"] for s in subagents],
            memory_virtual,
        )
        return agent, skill_roots


async def _build_from_live(
    db: AsyncSession,
    methodology: Methodology,
    settings: Settings,
) -> tuple[Any, list[Path]]:
    """从当前 live 表组装 Agent（会话版本 == 方法论当前 version）。"""
    return await _compile_from_agents(
        db,
        settings,
        list(methodology.agents),
        methodology_id=methodology.id,
        version=methodology.version,
        source="live",
        owner_user_id=methodology.owner_user_id,
        memory_content=read_project_memory(settings),
    )


async def _build_from_snapshot(
    db: AsyncSession,
    methodology_id: str,
    version: int,
    settings: Settings,
    *,
    owner_user_id: str,
) -> tuple[Any, list[Path]]:
    """从 MethodologyRevision 快照组装 Agent（旧会话锁定的历史版本）。"""
    revision = await get_revision(db, methodology_id, version)
    if revision is None:
        raise NotFoundError(
            f"方法论快照不存在：{methodology_id} v{version}（无法按旧会话版本重建）"
        )
    snapshot = await hydrate_snapshot_content(db, revision.snapshot or {})
    agents = list(snapshot.get("agents") or [])
    mem = snapshot.get("memory")
    if isinstance(mem, dict) and "content" in mem:
        memory_content = str(mem.get("content") or "")
    else:
        memory_content = ""
    return await _compile_from_agents(
        db,
        settings,
        agents,
        methodology_id=methodology_id,
        version=version,
        source="snapshot",
        owner_user_id=owner_user_id,
        memory_content=memory_content,
    )


async def _build_and_cache(
    db: AsyncSession,
    methodology: Methodology,
    *,
    owner_user_id: str,
    version: int,
    settings: Settings,
    key: str,
    use_cache: bool,
) -> Any:
    """按 live / snapshot 组装；成功后统一写入 LRU 缓存。"""
    if version == methodology.version:
        agent, skill_roots = await _build_from_live(db, methodology, settings)
    else:
        agent, skill_roots = await _build_from_snapshot(
            db,
            methodology.id,
            version,
            settings,
            owner_user_id=owner_user_id,
        )
    if use_cache:
        for evicted in _cache_put(key, agent, skill_roots=skill_roots):
            logger.info("Agent 缓存 LRU 淘汰：%s", evicted)
            _drop_build_lock(evicted)
    return agent


# ── 对外主入口 ────────────────────────────────────────────────────────


async def build_agent_from_methodology(
    db: AsyncSession,
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
    - 聊天路径通常同时传入 ``owner_user_id`` + ``version``：先查缓存，命中则不做
      方法论全量 eager-load

    Returns:
        LangGraph CompiledStateGraph
    """
    settings = settings or get_settings()
    methodology: Methodology | None = None

    # 调用方已带齐缓存键（典型 prepare_chat）：先查缓存，命中则不做 DB 查询
    if use_cache and owner_user_id is not None and version is not None:
        key = cache_key(owner_user_id, methodology_id, version)
        cached = await _cache_hit(key)
        if cached is not None:
            return cached
        owner = owner_user_id
        target_version = version
    else:
        methodology = await get_methodology_config(
            db,
            methodology_id,
            owner_user_id=owner_user_id,
        )
        owner = owner_user_id or methodology.owner_user_id
        target_version = version if version is not None else methodology.version
        key = cache_key(owner, methodology.id, target_version)
        if use_cache:
            cached = await _cache_hit(key)
            if cached is not None:
                return cached

    # 按 key 加锁：缓存 miss 时同进程并发请求只组装一次
    try:
        async with _build_lock_for(key):
            if use_cache:
                cached = await _cache_hit(key)
                if cached is not None:
                    return cached

            if methodology is None:
                methodology = await get_methodology_config(
                    db,
                    methodology_id,
                    owner_user_id=owner_user_id,
                )
            return await _build_and_cache(
                db,
                methodology,
                owner_user_id=owner,
                version=target_version,
                settings=settings,
                key=key,
                use_cache=use_cache,
            )
    except BaseException:
        await _cleanup_failed_build_lock(key)
        raise
