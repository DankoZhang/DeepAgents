"""
Agent 配置 API（全局 Agent）
============================

CRUD + Tool / Middleware 绑定。变更会 bump 所有勾选了该 Agent 的方法论版本。
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI：路由、依赖注入、HTTP 异常、查询参数
from fastapi import APIRouter, Depends, HTTPException, Query
# 请求级 ORM Session 类型
from sqlalchemy.orm import Session

# 请求/响应 Pydantic 模型
from deepagents_app.api.schemas import (
    AgentBindMiddlewares,  # POST .../middlewares 的 body
    AgentBindSkills,  # POST .../skills 的 body
    AgentBindTools,  # POST .../tools 的 body
    AgentCreate,  # POST /agent 的 body
    AgentOut,  # 统一响应形状
    AgentUpdate,  # PATCH /agent/{id} 的 body
)
# 每个请求注入 Session，结束时 commit/rollback
from deepagents_app.db.session import get_db
# 业务层：真正写库 / 查库
from deepagents_app.services import agents as agents_svc

# 本文件路由集合；OpenAPI 归到 agents 分组
router = APIRouter(tags=["agents"])


@router.get("/agent/list", response_model=list[AgentOut])  # 列表；响应为 AgentOut 数组
def list_agents(
    # 可选过滤：只返回某方法论已勾选的 Agent（勾选 UI 也可用全量 list）
    methodology_id: str | None = Query(
        None, description="若指定则只返回该方法论已勾选的 Agent"
    ),
    db: Session = Depends(get_db),  # 自动注入 DB 会话
):
    # 委托 service；FastAPI 再按 AgentOut 序列化（含 tools/middlewares）
    return agents_svc.list_agents(db, methodology_id=methodology_id)


@router.get("/agent/{agent_id}", response_model=AgentOut)  # 按 id 取单个详情
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    # 路径参数 agent_id → service 查询（含 joinedload 绑定）
    row = agents_svc.get_agent(db, agent_id)
    if row is None:
        # 不存在统一 404
        raise HTTPException(status_code=404, detail="Agent 不存在")
    # ORM 行 → AgentOut JSON
    return row


@router.post("/agent", response_model=AgentOut)  # 创建全局 Agent
def create_agent(body: AgentCreate, db: Session = Depends(get_db)):
    """创建全局 Agent；``config.role`` 取 supervisor / subagent。"""
    try:
        # 拆 body 字段传给 service（不在路由里写库）
        return agents_svc.create_agent(
            db,
            name=body.name,  # 全局唯一名
            system_prompt=body.system_prompt,  # 系统提示词
            model_id=body.model_id,  # 目录模型；缺省 model_default
            config=body.config,  # role / description / enabled 等
            agent_id=body.id,  # 可选客户端指定主键
            tool_ids=body.tool_ids,  # 创建时一并绑定工具
            middleware_ids=body.middleware_ids,  # 创建时一并绑定中间件
            skill_ids=body.skill_ids,  # 创建时一并绑定 Skill
        )
    except ValueError as exc:
        # 如重名 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/agent/{agent_id}", response_model=AgentOut)  # 部分更新
def update_agent(agent_id: str, body: AgentUpdate, db: Session = Depends(get_db)):
    try:
        return agents_svc.update_agent(
            db,
            agent_id,  # 路径上的目标 id
            name=body.name,  # None 表示不改该字段
            system_prompt=body.system_prompt,
            model_id=body.model_id,
            config=body.config,  # service 内与旧 config merge
            tool_ids=body.tool_ids,  # 传入则整表替换工具绑定
            middleware_ids=body.middleware_ids,  # 传入则整表替换中间件绑定
            skill_ids=body.skill_ids,
        )
    except LookupError as exc:
        # Agent 不存在 → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 如改名撞车 → 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/agent/{agent_id}")  # 删除全局 Agent
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    try:
        # service：级联清关系，并 bump 曾勾选它的方法论
        agents_svc.delete_agent(db, agent_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    # 无复杂 body，返回简单成功标记
    return {"ok": True}


@router.post("/agent/{agent_id}/tools", response_model=AgentOut)  # 单独改工具绑定
def bind_tools(agent_id: str, body: AgentBindTools, db: Session = Depends(get_db)):
    try:
        return agents_svc.bind_agent_tools(
            db, agent_id, body.tool_ids, replace=body.replace  # replace=True 先清空再绑
        )
    except LookupError as exc:
        # Agent 或某个 tool_id 不存在 → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agent/{agent_id}/middlewares", response_model=AgentOut)  # 单独改中间件绑定
def bind_middlewares(
    agent_id: str,  # 路径参数
    body: AgentBindMiddlewares,  # middleware_ids + replace
    db: Session = Depends(get_db),
):
    try:
        return agents_svc.bind_agent_middlewares(
            db, agent_id, body.middleware_ids, replace=body.replace
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/agent/{agent_id}/skills", response_model=AgentOut)
def bind_skills(agent_id: str, body: AgentBindSkills, db: Session = Depends(get_db)):
    try:
        return agents_svc.bind_agent_skills(
            db, agent_id, body.skill_ids, replace=body.replace
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
