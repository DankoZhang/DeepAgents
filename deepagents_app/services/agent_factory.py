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

# 推迟注解求值，便于类型提示中的前向引用
from __future__ import annotations

# 记录缓存命中、版本不一致、组装过程等日志
import logging
# Lock：保护进程内 _cache 的并发读写
import threading
# Any：Compiled Agent、中间件实例、工具实例等运行时类型
from typing import Any

# Session：DB 会话；joinedload：预加载方法论下的 agents/tools/middlewares
from sqlalchemy.orm import Session, joinedload

# 按 Settings 构建 FilesystemBackend（workspace 根）
from deepagents_app.backends import build_filesystem_backend
# Settings 类型与全局配置单例
from deepagents_app.config import Settings, get_settings
# ORM：Agent 定义、方法论、中间件定义（快照路径按 id 再查）
from deepagents_app.db.models import (
    AgentDefinition,  # live 路径上的全局 Agent 行
    Methodology,  # 方法论 live 行（含 version）
    MiddlewareDefinition,  # 快照路径按 middleware_ids 加载
)
# create_deep_agent 周边：检查点、HITL、权限、通用画像、workspace 同步
from deepagents_app.factory import (
    build_checkpointer,  # LangGraph checkpointer（会话状态持久化）
    build_interrupt_on,  # 全局 HITL 中断配置
    build_permissions,  # 文件系统等权限策略
    configure_general_purpose_profile,  # 配置 general-purpose 子 Agent 画像
    sync_memory_and_skills_into_workspace,  # 把 AGENTS.md / skills 同步进 workspace
)
# 按 Settings + 可选覆盖构造聊天模型
from deepagents_app.models import build_chat_model
# 将 MiddlewareDefinition ORM 行实例化为中间件对象
from deepagents_app.registries.middleware import load_middleware_object
# 展开 ToolDefinition；快照路径按 tool_ids 批量加载工具
from deepagents_app.registries.tools import expand_tool_definition, load_tools_by_ids
# 按方法论 id + version 取历史快照行
from deepagents_app.services.revisions import get_revision

# 本模块日志器
logger = logging.getLogger(__name__)

# 进程内 Compiled Agent 缓存：key → create_deep_agent 返回值
_cache: dict[str, Any] = {}
# 保护 _cache 读写，避免多线程同时组装/失效时竞态
_cache_lock = threading.Lock()


def cache_key(methodology_id: str, version: int) -> str:
    """生成缓存键：方法论 id + 版本号。"""
    return f"{methodology_id}:v{version}"  # 例：meth_xxx:v3


def invalidate_agent_cache(
    methodology_id: str | None = None,  # 指定则只清该方法论相关缓存
    version: int | None = None,  # 与 id 同传则只删一条；仅 id 则删所有版本
) -> int:
    """
    失效缓存。

    - 指定 id+version：删一条
    - 仅指定 id：删该方法论所有版本
    - 都不指定：清空
    """
    with _cache_lock:  # 持锁修改 _cache
        if methodology_id is None:
            # 全量清空：返回被删条数
            n = len(_cache)
            _cache.clear()
            return n
        if version is not None:
            # 精确删一条：存在返回 1，否则 0
            key = cache_key(methodology_id, version)
            return 1 if _cache.pop(key, None) is not None else 0
        # 只给了方法论 id：删所有以 "{id}:v" 开头的键
        prefix = f"{methodology_id}:v"
        to_del = [k for k in _cache if k.startswith(prefix)]
        for k in to_del:
            del _cache[k]
        return len(to_del)  # 实际删除条数


def get_methodology_config(
    db: Session,  # 当前事务会话
    methodology_id: str,  # 方法论主键
    *,  # 其后必须关键字传参
    version: int | None = None,  # 可选：与 live.version 比对，不一致仅打日志
) -> Methodology:
    """查询方法论 live 行（含 agents / tools / middlewares）。"""
    methodology = (
        db.query(Methodology)  # 从 methodology 表起查
        .options(
            # 预加载 agents，并再预加载每个 Agent 的 tools（避免 N+1）
            joinedload(Methodology.agents).joinedload(AgentDefinition.tools),
            # 同理预加载每个 Agent 的 middlewares
            joinedload(Methodology.agents).joinedload(AgentDefinition.middlewares),
        )
        .filter(Methodology.id == methodology_id)  # WHERE id = :methodology_id
        .one_or_none()  # 0 行 None；1 行对象；>1 抛错
    )
    if methodology is None:
        raise LookupError(f"方法论不存在：{methodology_id}")  # 交给上层转 404
    # 调用方指定了历史版本且与 live 不一致：提示后续可能走快照路径
    if version is not None and methodology.version != version:
        logger.warning(
            "请求版本 v%s 与当前方法论版本 v%s 不一致，将尝试快照重建",
            version,
            methodology.version,
        )
    return methodology  # 始终返回 live 行（版本一致性由 build 入口再判断）


def _agent_role(agent: AgentDefinition | dict[str, Any]) -> str:
    """读取 Agent 角色：supervisor / subagent（默认 subagent）。"""
    if isinstance(agent, dict):
        # 快照路径：agent 是 JSON dict
        cfg = agent.get("config") or {}
    else:
        # live 路径：AgentDefinition ORM，config 为 JSON 列
        cfg = agent.config or {}
    return str(cfg.get("role", "subagent")).lower()  # 缺省子 Agent，统一小写比较


def _agent_enabled(agent: AgentDefinition | dict[str, Any]) -> bool:
    """是否启用：enabled=false 时组装阶段跳过。"""
    if isinstance(agent, dict):
        cfg = agent.get("config") or {}  # 快照 dict
    else:
        cfg = agent.config or {}  # ORM 行
    return bool(cfg.get("enabled", True))  # 缺省启用


def _load_middlewares_by_ids(db: Session, middleware_ids: list[str]) -> list[Any]:
    """按 id 列表加载并实例化中间件（保持传入顺序；缺失则跳过并告警）。"""
    if not middleware_ids:
        return []  # 无 id 直接空列表
    rows = (
        db.query(MiddlewareDefinition)  # 查中间件定义表
        .filter(MiddlewareDefinition.id.in_(middleware_ids))  # WHERE id IN (...)
        .all()  # 物化所有命中行
    )
    by_id = {r.id: r for r in rows}  # id → ORM，便于按原顺序取
    result: list[Any] = []
    for mid in middleware_ids:
        row = by_id.get(mid)
        if row is None:
            # 快照仍引用已删中间件：跳过，不中断整图组装
            logger.warning("快照引用的中间件不存在，跳过：%s", mid)
            continue
        result.append(load_middleware_object(row))  # ORM → 中间件实例
    return result


def _expand_agent_tools(agent: AgentDefinition) -> list[Any]:
    """把 Agent 已绑 ToolDefinition 全部展开为可调用工具列表。"""
    tools: list[Any] = []
    for t in agent.tools:
        # 单个 ToolDefinition 可能展开为多个底层 tool（如 MCP 包装）
        tools.extend(expand_tool_definition(t))
    return tools


def _build_subagent_spec_from_row(agent: AgentDefinition) -> dict[str, Any]:
    """AgentDefinition → deepagents SubAgent 字典。"""
    cfg = agent.config or {}  # 扩展 JSON：description / skills 等
    tools = _expand_agent_tools(agent)  # live：从关系表展开工具
    # live：从关系表加载并实例化中间件
    middleware = [load_middleware_object(m) for m in agent.middlewares]

    # create_deep_agent 要求的子 Agent 基础字段
    spec: dict[str, Any] = {
        "name": agent.name,  # 子 Agent 名称（调度用）
        "description": str(cfg.get("description") or agent.name),  # 缺省用名称
        "system_prompt": agent.system_prompt,  # 系统提示词
        "tools": tools,  # 已展开工具列表
    }
    skills = cfg.get("skills")
    if skills:
        spec["skills"] = list(skills)  # 可选 skills 路径列表
    if middleware:
        spec["middleware"] = middleware  # 有则挂上中间件链
    if agent.model:
        spec["model"] = agent.model  # 可选覆盖模型名
    return spec


def _build_subagent_spec_from_snapshot(
    db: Session,  # 仍需查 Tool / Middleware 定义表
    agent: dict[str, Any],  # 快照里的 Agent JSON
) -> dict[str, Any]:
    """快照 Agent dict → deepagents SubAgent 字典（按 id 再查工具/中间件）。"""
    cfg = agent.get("config") or {}  # 快照内嵌 config
    # 快照只存 tool_ids，运行时再解析为工具实例
    tools = load_tools_by_ids(db, list(agent.get("tool_ids") or []))
    # 同理按 middleware_ids 加载
    middleware = _load_middlewares_by_ids(db, list(agent.get("middleware_ids") or []))

    spec: dict[str, Any] = {
        "name": agent["name"],  # 快照必有 name
        "description": str(cfg.get("description") or agent["name"]),  # 缺省用名称
        "system_prompt": agent.get("system_prompt") or "",  # 缺省空串
        "tools": tools,
    }
    skills = cfg.get("skills")
    if skills:
        spec["skills"] = list(skills)
    if middleware:
        spec["middleware"] = middleware
    if agent.get("model"):
        spec["model"] = agent["model"]  # 可选模型覆盖
    return spec


def _assemble_create_kwargs(
    *,  # 全部关键字参数，避免 live/snapshot 传参错位
    settings: Settings,  # 全局运行时配置
    supervisor_prompt: str,  # Supervisor 系统提示词
    supervisor_name: str,  # Supervisor 显示名（亦作图 name）
    supervisor_model: str | None,  # 可选模型覆盖；None 用默认
    supervisor_temperature: float | None,  # 可选温度
    supervisor_tools: list[Any],  # Supervisor 工具列表（可空）
    supervisor_middleware: list[Any],  # Supervisor 中间件实例列表
    supervisor_config: dict[str, Any],  # 含 interrupt_on 等扩展
    subagents: list[dict[str, Any]],  # 已组装好的 SubAgent 规格列表
) -> dict[str, Any]:
    """
    live / snapshot 两条路径共用的 ``create_deep_agent`` 参数组装。

    HITL：方法论可覆盖 ``interrupt_on``；全局开关关闭时强制为 None。
    """
    # 方法论 Supervisor.config 可覆盖全局 HITL
    interrupt_cfg = supervisor_config.get("interrupt_on")
    if interrupt_cfg is None:
        # 未覆盖：走 Settings 全局 interrupt_on
        interrupt_on = build_interrupt_on(settings)
    else:
        # 有覆盖：仅当全局 enable_hitl 开启时生效，否则强制关闭
        interrupt_on = interrupt_cfg if settings.enable_hitl else None

    # 构造 Supervisor 所用聊天模型（可被 methodology Agent 字段覆盖）
    model = build_chat_model(
        settings,
        model_name=supervisor_model,  # None → Settings 默认模型
        temperature=supervisor_temperature,  # None → Settings 默认温度
    )

    backend = build_filesystem_backend(settings)  # workspace 文件系统后端
    checkpointer = build_checkpointer(settings)  # 会话 checkpoint 存储
    permissions = build_permissions()  # 工具/文件权限策略
    configure_general_purpose_profile(settings)  # 配置内置 general-purpose 画像
    # FilesystemBackend 根 = workspace，需先同步 AGENTS.md / skills
    sync_memory_and_skills_into_workspace(settings)
    # 仅当 workspace 下存在 AGENTS.md 时才传 memory 路径
    memory_paths = ["/AGENTS.md"] if (settings.workspace_dir / "AGENTS.md").exists() else None

    # create_deep_agent 公共参数
    create_kwargs: dict[str, Any] = {
        "model": model,  # Supervisor LLM
        "system_prompt": supervisor_prompt,  # Supervisor 系统提示
        "subagents": subagents,  # 子 Agent 规格列表
        "backend": backend,  # 文件系统后端
        "middleware": supervisor_middleware,  # Supervisor 中间件
        "memory": memory_paths,  # 可选记忆文件路径
        "permissions": permissions,  # 权限策略
        "interrupt_on": interrupt_on,  # HITL 中断配置（可为 None）
        "checkpointer": checkpointer,  # 状态检查点
        "name": supervisor_name,  # 编译图名称
    }
    if supervisor_tools:
        # 有工具才传入，避免空列表覆盖框架默认行为
        create_kwargs["tools"] = supervisor_tools
    return create_kwargs


def _build_from_live(
    db: Session,  # 已预加载 agents 关系的会话
    methodology: Methodology,  # live 方法论行
    settings: Settings,  # 运行时配置
) -> Any:
    """从当前 live 表组装 Agent（会话版本 == 方法论当前 version）。"""
    from deepagents import create_deep_agent  # 延迟导入，避免模块加载时拉起重依赖

    # 过滤掉 enabled=false 的 Agent
    agents = [a for a in methodology.agents if _agent_enabled(a)]
    # role=supervisor 的作为主控
    supervisors = [a for a in agents if _agent_role(a) == "supervisor"]
    # 其余一律当 SubAgent
    subagents_defs = [a for a in agents if _agent_role(a) != "supervisor"]

    if not supervisors:
        raise ValueError(f"方法论 {methodology.id} 缺少 Supervisor Agent")
    if len(supervisors) > 1:
        # 多 Supervisor 时取第一个，并告警
        logger.warning("方法论 %s 有多个 Supervisor，使用第一个", methodology.id)
    supervisor = supervisors[0]

    # live ORM → SubAgent 规格
    subagents = [_build_subagent_spec_from_row(a) for a in subagents_defs]
    # Supervisor 中间件：关系表 → 实例
    middleware = [load_middleware_object(m) for m in supervisor.middlewares]
    # Supervisor 工具：关系表展开
    supervisor_tools = _expand_agent_tools(supervisor)

    logger.info(
        "动态组装 Agent（live）：methodology=%s v%s supervisor=%s subagents=%s",
        methodology.id,
        methodology.version,
        supervisor.name,
        [s["name"] for s in subagents],
    )

    # 组装 create_deep_agent 参数（与 snapshot 路径共用）
    kwargs = _assemble_create_kwargs(
        settings=settings,
        supervisor_prompt=supervisor.system_prompt,  # ORM 系统提示词
        supervisor_name=supervisor.name,  # ORM 名称
        supervisor_model=supervisor.model,  # 可为 None
        supervisor_temperature=supervisor.temperature,  # 可为 None
        supervisor_tools=supervisor_tools,
        supervisor_middleware=middleware,
        supervisor_config=dict(supervisor.config or {}),  # 拷贝，避免副作用
        subagents=subagents,
    )
    return create_deep_agent(**kwargs)  # 返回 Compiled Agent


def _build_from_snapshot(
    db: Session,  # 用于按 id 再查工具/中间件定义
    methodology_id: str,  # 方法论主键
    version: int,  # 历史版本号
    settings: Settings,  # 运行时配置
) -> Any:
    """从 MethodologyRevision 快照组装 Agent（旧会话锁定历史版本）。"""
    from deepagents import create_deep_agent  # 延迟导入

    # 取指定版本的修订快照行
    revision = get_revision(db, methodology_id, version)
    if revision is None:
        raise LookupError(
            f"方法论快照不存在：{methodology_id} v{version}（无法按旧会话版本重建）"
        )
    snapshot = revision.snapshot or {}  # JSON：含 agents 等
    # 快照 agents 为 dict 列表；同样过滤 enabled=false
    agents = [a for a in snapshot.get("agents", []) if _agent_enabled(a)]
    supervisors = [a for a in agents if _agent_role(a) == "supervisor"]
    subagents_defs = [a for a in agents if _agent_role(a) != "supervisor"]

    if not supervisors:
        raise ValueError(f"快照 {methodology_id} v{version} 缺少 Supervisor Agent")
    supervisor = supervisors[0]  # 快照路径不额外告警多 Supervisor

    # 快照 dict → SubAgent 规格（内部再查 tool/middleware）
    subagents = [_build_subagent_spec_from_snapshot(db, a) for a in subagents_defs]
    # Supervisor 中间件：快照只存 ids
    middleware = _load_middlewares_by_ids(
        db, list(supervisor.get("middleware_ids") or [])
    )
    # Supervisor 工具：按 tool_ids 加载
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
        supervisor_prompt=supervisor.get("system_prompt") or "",  # 缺省空串
        supervisor_name=str(supervisor.get("name") or "supervisor"),  # 缺省占位名
        supervisor_model=supervisor.get("model"),
        supervisor_temperature=supervisor.get("temperature"),
        supervisor_tools=supervisor_tools,
        supervisor_middleware=middleware,
        supervisor_config=dict(supervisor.get("config") or {}),  # 拷贝 config
        subagents=subagents,
    )
    return create_deep_agent(**kwargs)


def build_agent_from_methodology(
    db: Session,  # 当前事务会话
    methodology_id: str,  # 方法论主键
    *,  # 其后必须关键字传参
    version: int | None = None,  # None：跟 live；有值：按会话锁定版本
    settings: Settings | None = None,  # None：读全局 get_settings()
    use_cache: bool = True,  # False：强制重新组装（调试用）
) -> Any:
    """
    根据方法论动态创建 Compiled Agent。

    Returns:
        LangGraph CompiledStateGraph
    """
    settings = settings or get_settings()  # 未传入则用进程全局配置
    # 始终先取 live 行（含关系预加载）；version 仅用于比对/选路径
    methodology = get_methodology_config(db, methodology_id, version=version)
    # 未指定 version 时跟 live；指定时优先保证会话创建时的版本一致性
    target_version = version if version is not None else methodology.version
    key = cache_key(methodology.id, target_version)  # 缓存键

    if use_cache:
        with _cache_lock:
            cached = _cache.get(key)
            if cached is not None:
                logger.info("命中 Agent 缓存：%s", key)
                return cached  # 直接返回已编译图，跳过组装

    # 版本一致走 live（含关系预加载）；否则必须从快照重建
    if target_version == methodology.version:
        agent = _build_from_live(db, methodology, settings)
    else:
        agent = _build_from_snapshot(db, methodology.id, target_version, settings)

    if use_cache:
        with _cache_lock:
            _cache[key] = agent  # 写入缓存供后续会话复用
    return agent
