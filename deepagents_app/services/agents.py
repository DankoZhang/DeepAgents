"""Agent 配置管理：全局 Agent CRUD + 绑定 Tool / Middleware。"""

# 推迟注解求值，便于类型提示中的前向引用
from __future__ import annotations

# 生成 Agent 主键后缀（未显式传 agent_id 时）
import uuid
# timezone.utc：写入带时区的更新时间
from datetime import datetime, timezone
# Any：config JSON 等任意结构
from typing import Any

# Session：DB 会话；joinedload：预加载关系，避免 N+1 查询
from sqlalchemy.orm import Session, joinedload

# ORM 表：Agent、关系表、方法论、工具、中间件
from deepagents_app.db.models import (
    AgentDefinition,  # 全局 Agent 主表
    AgentMiddleware,  # Agent ↔ Middleware 多对多
    AgentSkill,  # Agent ↔ Skill 多对多
    AgentTool,  # Agent ↔ Tool 多对多
    Methodology,  # 方法论（版本 bump 时用）
    MethodologyAgent,  # 方法论勾选全局 Agent
    MiddlewareDefinition,  # 校验中间件 id 是否存在
    ModelDefinition,  # 方案 B：模型目录
    SkillDefinition,  # 校验 Skill id 是否存在
    ToolDefinition,  # 校验工具 id 是否存在
)
# Agent 配置变更后清空已编译图缓存，避免继续用旧绑定
from deepagents_app.services.agent_factory import invalidate_agent_cache
# 方法论 version+1 后写入可重建的 JSON 快照
from deepagents_app.services.revisions import snapshot_methodology


def list_agents(
    db: Session,  # 当前事务会话
    *,  # 其后必须关键字传参
    methodology_id: str | None = None,  # 若指定则只返回该方法论已勾选的 Agent
) -> list[AgentDefinition]:
    """列出全局 Agent；若传 methodology_id 则只返回该方法论已勾选的。"""
    # 查 Agent，并预加载 tools / middlewares / skills / llm_model 供序列化为 AgentOut
    q = db.query(AgentDefinition).options(
        joinedload(AgentDefinition.tools),  # 一次查出已绑工具，避免懒加载 N+1
        joinedload(AgentDefinition.middlewares),  # 一次查出已绑中间件
        joinedload(AgentDefinition.skills),  # 一次查出已绑 Skill
        joinedload(AgentDefinition.llm_model),  # 方案 B：目录模型
    )
    # 通过中间表过滤：只保留被该方法论勾选的行
    if methodology_id:
        q = q.join(MethodologyAgent).filter(  # INNER JOIN methodology_agent
            MethodologyAgent.methodology_id == methodology_id  # 限定方法论
        )
    # 按名称排序后物化结果列表
    return q.order_by(AgentDefinition.name).all()


def get_agent(db: Session, agent_id: str) -> AgentDefinition | None:
    """
    按主键取单个全局 Agent，并带上最新的 tools / middlewares。

    典型调用：create/update/bind 之后返回给 API，或路由 GET /agent/{id}。
    """
    # 清空本 Session identity map 中已缓存对象的过期标记，避免仍拿着绑定前的旧关系集合
    db.expire_all()
    return (
        # 从 agent_definition 起查
        db.query(AgentDefinition)
        .options(
            joinedload(AgentDefinition.tools),  # JOIN 加载已绑定 ToolDefinition 列表
            joinedload(AgentDefinition.middlewares),  # JOIN 加载已绑定 MiddlewareDefinition 列表
            joinedload(AgentDefinition.skills),  # JOIN 加载已绑定 SkillDefinition 列表
            joinedload(AgentDefinition.llm_model),
        )
        .filter(AgentDefinition.id == agent_id)  # WHERE id = :agent_id
        .one_or_none()  # 找到 1 行返回对象；0 行返回 None；>1 行抛错
    )


def create_agent(
    db: Session,
    *,  # 其后必须关键字传参，防止参数错位
    name: str,  # 全局唯一显示名
    system_prompt: str = "",  # 系统提示词
    model_id: str | None = None,  # 方案 B：绑定模型目录
    model: str | None = None,  # 兼容：无目录时的模型名兜底
    temperature: float | None = None,  # 兼容：无目录时的温度
    config: dict[str, Any] | None = None,  # role / description / enabled 等扩展
    agent_id: str | None = None,  # 可选固定主键（种子用）
    tool_ids: list[str] | None = None,  # 创建时一并绑定的工具
    middleware_ids: list[str] | None = None,  # 创建时一并绑定的中间件
    skill_ids: list[str] | None = None,  # 创建时一并绑定的 Skill
    bump_related: bool = True,  # False：批量种子时跳过版本 bump
) -> AgentDefinition:
    """创建全局 Agent（不隶属单一方法论；由方法论另行勾选）。"""
    # 名称全局唯一校验
    existing = (
        db.query(AgentDefinition)  # 查 Agent 表
        .filter(AgentDefinition.name == name)  # 同名条件
        .one_or_none()  # 已有则拿到行，没有则 None
    )
    if existing is not None:
        raise ValueError(f"已存在同名 Agent：{name}")  # 交给路由转 400

    resolved_model_id = _validate_model_id(db, model_id)

    # 拷贝 config，避免改到调用方传入的原 dict
    cfg = dict(config or {})
    # skills 已改走 agent_skill；丢掉遗留路径列表，避免与关系表双源
    cfg.pop("skills", None)
    # 缺省角色为子 Agent；Supervisor 需显式传 role=supervisor
    cfg.setdefault("role", "subagent")
    # 缺省启用；enabled=false 时 Factory 组装会跳过
    cfg.setdefault("enabled", True)

    # 构造 ORM 行（尚未关联方法论）
    row = AgentDefinition(
        id=agent_id or f"agent_{uuid.uuid4().hex[:12]}",  # 无指定则生成 agent_<hex>
        name=name,  # 写入显示名
        system_prompt=system_prompt,  # 写入系统提示词
        model_id=resolved_model_id,
        model=model,  # 可为 None
        temperature=temperature,  # 可为 None
        config=cfg,  # 写入合并后的扩展 JSON
    )
    db.add(row)  # 纳入当前 Session（pending insert）
    db.flush()  # 执行 INSERT，拿到约束校验，后续绑定可用 row.id

    # 若传入工具列表则整表替换绑定（新建时等价于初次写入）
    if tool_ids:
        _bind_tools(db, row.id, tool_ids, replace=True)
    # 同理绑定中间件
    if middleware_ids:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)
    if skill_ids:
        _bind_skills(db, row.id, skill_ids, replace=True)

    # 新建时通常还没被方法论勾选；若已有关联则 bump 那些方法论
    if bump_related:
        _bump_methodologies_using_agent(db, row.id)
    # 用 get_agent 重读，保证返回值含 joinedload 的最新关系
    return get_agent(db, row.id)  # type: ignore[return-value]


def update_agent(
    db: Session,
    agent_id: str,  # 要更新的 Agent 主键
    *,
    name: str | None = None,  # None 表示该字段不改
    system_prompt: str | None = None,  # None 不改
    model_id: str | None = None,  # None 不改；配合 clear_model_id
    clear_model_id: bool = False,
    model: str | None = None,  # None 不改
    temperature: float | None = None,  # None 不改
    config: dict[str, Any] | None = None,  # 与现有 config 做 merge
    tool_ids: list[str] | None = None,  # 传入则整表替换工具绑定
    middleware_ids: list[str] | None = None,  # 传入则整表替换中间件绑定
    skill_ids: list[str] | None = None,  # 传入则整表替换 Skill 绑定
    bump_related: bool = True,  # 是否 bump 所有勾选了该 Agent 的方法论
) -> AgentDefinition:
    # 加载含关系的当前行（内部会 expire_all）
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")  # 路由转 404

    # 改名时检查是否与其他 Agent 撞名
    if name is not None and name != row.name:
        clash = (
            db.query(AgentDefinition)
            .filter(AgentDefinition.name == name, AgentDefinition.id != agent_id)  # 排除自己
            .one_or_none()
        )
        if clash is not None:
            raise ValueError(f"已存在同名 Agent：{name}")
        row.name = name  # 写入新名
    # 选择性更新各标量字段
    if system_prompt is not None:
        row.system_prompt = system_prompt
    if clear_model_id:
        row.model_id = None
    elif model_id is not None:
        row.model_id = _validate_model_id(db, model_id)
    if model is not None:
        row.model = model or None  # 空串视为清除，回退默认模型
    if temperature is not None:
        row.temperature = temperature
    if config is not None:
        # 浅合并：保留未在本次 patch 中出现的旧键
        merged = dict(row.config or {})
        merged.update(config)  # 本次传入的键覆盖同名旧键
        merged.pop("skills", None)  # 路径式 skills 已废弃
        row.config = merged
    if tool_ids is not None:
        _bind_tools(db, row.id, tool_ids, replace=True)  # 整表替换工具关系
    if middleware_ids is not None:
        _bind_middlewares(db, row.id, middleware_ids, replace=True)  # 整表替换中间件关系
    if skill_ids is not None:
        _bind_skills(db, row.id, skill_ids, replace=True)

    db.flush()  # 把脏字段与关系变更刷到 DB
    # 全局 Agent 被多个方法论共享：任一变更都要让相关方法论升版并快照
    if bump_related:
        _bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def delete_agent(db: Session, agent_id: str, *, bump_related: bool = True) -> None:
    # 按主键取行（可不预加载关系）
    row = db.get(AgentDefinition, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    # 删除前先记下勾选了它的方法论，删后关系行会 cascade 掉
    methodology_ids = [
        r.methodology_id  # 收集方法论 id
        for r in db.query(MethodologyAgent)  # 查勾选关系表
        .filter(MethodologyAgent.agent_id == agent_id)  # 该 Agent 的全部勾选
        .all()
    ]
    db.delete(row)  # 级联删除 agent_tool / agent_middleware / methodology_agent
    db.flush()  # 执行 DELETE
    # 对曾引用该 Agent 的方法论升版，使旧会话仍可按快照重建
    if bump_related:
        for mid in methodology_ids:
            methodology = db.get(Methodology, mid)  # 再取方法论行
            if methodology:
                _bump_and_snapshot(db, methodology)  # version+1 + 快照 + 清缓存


def bind_agent_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],  # 目标工具 id 列表
    *,
    replace: bool = True,  # True 清空再绑；False 增量追加
    bump_related: bool = True,  # 是否 bump 相关方法论
) -> AgentDefinition:
    # 确认 Agent 存在（顺带 expire + 预加载）
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    _bind_tools(db, agent_id, tool_ids, replace=replace)  # 写 agent_tool
    db.flush()
    if bump_related:
        _bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def bind_agent_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],  # 目标中间件 id 列表
    *,
    replace: bool = True,  # True 清空再绑；False 增量追加
    bump_related: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    _bind_middlewares(db, agent_id, middleware_ids, replace=replace)  # 写 agent_middleware
    db.flush()
    if bump_related:
        _bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def bind_agent_skills(
    db: Session,
    agent_id: str,
    skill_ids: list[str],
    *,
    replace: bool = True,
    bump_related: bool = True,
) -> AgentDefinition:
    row = get_agent(db, agent_id)
    if row is None:
        raise LookupError(f"Agent 不存在：{agent_id}")
    _bind_skills(db, agent_id, skill_ids, replace=replace)
    db.flush()
    if bump_related:
        _bump_methodologies_using_agent(db, agent_id)
    return get_agent(db, agent_id)  # type: ignore[return-value]


def _bind_tools(
    db: Session,
    agent_id: str,
    tool_ids: list[str],
    *,
    replace: bool,  # 是否先删光该 Agent 已有工具关系
) -> None:
    # 替换模式：删除 agent_tool 中该 agent 的全部行
    if replace:
        db.query(AgentTool).filter(AgentTool.agent_id == agent_id).delete()
    # 逐个校验工具存在并写入关系（跳过已存在，支持增量）
    for tid in tool_ids:
        tool = db.get(ToolDefinition, tid)  # 校验工具主键
        if tool is None:
            raise LookupError(f"工具不存在：{tid}")
        exists = (
            db.query(AgentTool)
            .filter(AgentTool.agent_id == agent_id, AgentTool.tool_id == tid)  # 复合主键查重
            .one_or_none()
        )
        if exists is None:
            db.add(AgentTool(agent_id=agent_id, tool_id=tid))  # 插入新关系行


def _bind_middlewares(
    db: Session,
    agent_id: str,
    middleware_ids: list[str],
    *,
    replace: bool,  # 是否先清空旧绑定
) -> None:
    # 替换模式：清空该 Agent 的中间件关系
    if replace:
        db.query(AgentMiddleware).filter(AgentMiddleware.agent_id == agent_id).delete()
    for mid in middleware_ids:
        mw = db.get(MiddlewareDefinition, mid)  # 校验中间件存在
        if mw is None:
            raise LookupError(f"中间件不存在：{mid}")
        # 增量时避免重复插入复合主键
        exists = (
            db.query(AgentMiddleware)
            .filter(
                AgentMiddleware.agent_id == agent_id,
                AgentMiddleware.middleware_id == mid,
            )
            .one_or_none()
        )
        if exists is None:
            db.add(AgentMiddleware(agent_id=agent_id, middleware_id=mid))


def _bind_skills(
    db: Session,
    agent_id: str,
    skill_ids: list[str],
    *,
    replace: bool,
) -> None:
    if replace:
        db.query(AgentSkill).filter(AgentSkill.agent_id == agent_id).delete()
    for sid in skill_ids:
        skill = db.get(SkillDefinition, sid)
        if skill is None:
            raise LookupError(f"Skill 不存在：{sid}")
        if skill.status != "active":
            raise ValueError(f"Skill 已禁用：{skill.name}")
        exists = (
            db.query(AgentSkill)
            .filter(AgentSkill.agent_id == agent_id, AgentSkill.skill_id == sid)
            .one_or_none()
        )
        if exists is None:
            db.add(AgentSkill(agent_id=agent_id, skill_id=sid))


def _validate_model_id(db: Session, model_id: str | None) -> str | None:
    """校验 model_id 存在且可用；None 表示不绑定目录。"""
    if not model_id:
        return None
    row = db.get(ModelDefinition, model_id)
    if row is None:
        raise LookupError(f"模型不存在：{model_id}")
    if row.status != "active":
        raise ValueError(f"模型已禁用：{row.name}")
    return model_id


def _bump_methodologies_using_agent(db: Session, agent_id: str) -> None:
    """找出勾选了该全局 Agent 的全部方法论，逐个 version+1 并快照。"""
    links = (
        db.query(MethodologyAgent)  # 方法论 ↔ Agent 关系
        .filter(MethodologyAgent.agent_id == agent_id)
        .all()  # 该 Agent 被哪些方法论勾选
    )
    for link in links:
        methodology = db.get(Methodology, link.methodology_id)  # 取方法论实体
        if methodology:
            _bump_and_snapshot(db, methodology)  # 升版 + 快照 + 清缓存


def _bump_and_snapshot(db: Session, methodology: Methodology) -> None:
    """配置变更收尾：升版 → 失效缓存 → 写当前 version 的快照。"""
    methodology.version += 1  # 旧会话仍按 Conversation.methodology_version 读历史快照
    methodology.updated_time = datetime.now(timezone.utc)  # 刷新更新时间
    invalidate_agent_cache(methodology.id)  # 删该方法论各版本的 Compiled Agent 缓存
    db.flush()  # 先让 version 落库，再按新 version 写快照行
    snapshot_methodology(db, methodology.id)  # 序列化当前配置写入 methodology_revision
