"""
Agent 配置 API（全局 Agent）
============================

CRUD + Tool / Middleware / Skill 绑定。变更会 bump 所有勾选了该 Agent 的方法论版本。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import (
    AgentBindMiddlewares,
    AgentBindSkills,
    AgentBindTools,
    AgentCreate,
    AgentOut,
    AgentUpdate,
)
from deepagents_app.db.session import get_db
from deepagents_app.services import agents as agents_svc

router = APIRouter(tags=["agents"])


@router.get("/agent/list", response_model=list[AgentOut])
def list_agents(
    methodology_id: str | None = Query(
        None, description="若指定则只返回该方法论已勾选的 Agent"
    ),
    db: Session = Depends(get_db),
):
    return agents_svc.list_agents(db, methodology_id=methodology_id)


@router.get("/agent/{agent_id}", response_model=AgentOut)
def get_agent(agent_id: str, db: Session = Depends(get_db)):
    row = agents_svc.get_agent(db, agent_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Agent 不存在")
    return row


@router.post("/agent", response_model=AgentOut)
def create_agent(body: AgentCreate, db: Session = Depends(get_db)):
    """创建全局 Agent；``config.role`` 取 supervisor / subagent。"""
    return agents_svc.create_agent(
        db,
        name=body.name,
        system_prompt=body.system_prompt,
        model_id=body.model_id,
        config=body.config,
        agent_id=body.id,
        tool_ids=body.tool_ids,
        middleware_ids=body.middleware_ids,
        skill_ids=body.skill_ids,
    )


@router.patch("/agent/{agent_id}", response_model=AgentOut)
def update_agent(agent_id: str, body: AgentUpdate, db: Session = Depends(get_db)):
    return agents_svc.update_agent(
        db,
        agent_id,
        name=body.name,
        system_prompt=body.system_prompt,
        model_id=body.model_id,
        config=body.config,
        tool_ids=body.tool_ids,
        middleware_ids=body.middleware_ids,
        skill_ids=body.skill_ids,
    )


@router.delete("/agent/{agent_id}")
def delete_agent(agent_id: str, db: Session = Depends(get_db)):
    agents_svc.delete_agent(db, agent_id)
    return {"ok": True}


@router.post("/agent/{agent_id}/tools", response_model=AgentOut)
def bind_tools(agent_id: str, body: AgentBindTools, db: Session = Depends(get_db)):
    return agents_svc.bind_agent_tools(
        db, agent_id, body.tool_ids, replace=body.replace
    )


@router.post("/agent/{agent_id}/middlewares", response_model=AgentOut)
def bind_middlewares(
    agent_id: str,
    body: AgentBindMiddlewares,
    db: Session = Depends(get_db),
):
    return agents_svc.bind_agent_middlewares(
        db, agent_id, body.middleware_ids, replace=body.replace
    )


@router.post("/agent/{agent_id}/skills", response_model=AgentOut)
def bind_skills(agent_id: str, body: AgentBindSkills, db: Session = Depends(get_db)):
    return agents_svc.bind_agent_skills(
        db, agent_id, body.skill_ids, replace=body.replace
    )
