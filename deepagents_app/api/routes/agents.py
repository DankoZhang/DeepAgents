"""
Agent 配置 API
==============

CRUD + Tool / Middleware / Skill 绑定。变更会 bump 所有勾选了该 Agent 的方法论版本。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.api.errors import require_entity
from deepagents_app.api.pagination import (
    cursor_query,
    limit_query,
    offset_query,
    set_next_cursor,
    set_total_count,
)
from deepagents_app.api.schemas import (
    AgentBindMiddlewares,
    AgentBindSkills,
    AgentBindTools,
    AgentCreate,
    AgentOut,
    AgentUpdate,
)
from deepagents_app.db.session import get_async_db
from deepagents_app.services.catalog import agents as agents_svc
router = APIRouter(tags=["agents"])


@router.get("/agent/list", response_model=list[AgentOut])
async def list_agents(
    response: Response,
    methodology_id: str | None = Query(
        None, description="若指定则只返回该方法论已勾选的 Agent"
    ),
    limit: int = Depends(limit_query),
    offset: int = Depends(offset_query),
    cursor: str | None = Depends(cursor_query),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    rows, total, next_cursor = await agents_svc.list_agents(
        db,
        owner_user_id=user_id,
        methodology_id=methodology_id,
        limit=limit,
        offset=offset,
        cursor=cursor,
    )
    set_total_count(response, total)
    set_next_cursor(response, next_cursor)
    return rows


@router.get("/agent/{agent_id}", response_model=AgentOut)
async def get_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    row = await agents_svc.get_agent(db, agent_id, owner_user_id=user_id)
    return require_entity(row, "Agent 不存在")


@router.post("/agent", response_model=AgentOut)
async def create_agent(
    body: AgentCreate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """创建全局 Agent；``config.role`` 取 supervisor / subagent。"""
    return await agents_svc.create_agent(
        db,
        owner_user_id=user_id,
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
async def update_agent(
    agent_id: str,
    body: AgentUpdate,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await agents_svc.update_agent(
        db,
        agent_id,
        owner_user_id=user_id,
        name=body.name,
        system_prompt=body.system_prompt,
        model_id=body.model_id,
        config=body.config,
        tool_ids=body.tool_ids,
        middleware_ids=body.middleware_ids,
        skill_ids=body.skill_ids,
    )


@router.delete("/agent/{agent_id}")
async def delete_agent(
    agent_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    await agents_svc.delete_agent(db, agent_id, owner_user_id=user_id)
    return {"ok": True}


@router.post("/agent/{agent_id}/tools", response_model=AgentOut)
async def bind_tools(
    agent_id: str,
    body: AgentBindTools,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await agents_svc.bind_agent_tools(
        db,
        agent_id,
        body.tool_ids,
        owner_user_id=user_id,
        replace=body.replace,
    )


@router.post("/agent/{agent_id}/middlewares", response_model=AgentOut)
async def bind_middlewares(
    agent_id: str,
    body: AgentBindMiddlewares,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await agents_svc.bind_agent_middlewares(
        db,
        agent_id,
        body.middleware_ids,
        owner_user_id=user_id,
        replace=body.replace,
    )


@router.post("/agent/{agent_id}/skills", response_model=AgentOut)
async def bind_skills(
    agent_id: str,
    body: AgentBindSkills,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    return await agents_svc.bind_agent_skills(
        db,
        agent_id,
        body.skill_ids,
        owner_user_id=user_id,
        replace=body.replace,
    )
