"""
快照序列化
==========

live ORM → 可持久化 JSON 的唯一入口。
``revisions``（落库）与 ``agent_factory``（组装归一）共用，避免两处漂移。

落库快照中 Skill / system_prompt / Memory 正文改为 content_blob 哈希引用；
组装前由 ``hydrate_snapshot_content`` 还原明文。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.db.models import (
    AgentDefinition,
    MiddlewareDefinition,
    SkillDefinition,
    ToolDefinition,
)
from deepagents_app.services.versioning.content_blobs import ensure_content_blob
from deepagents_app.services.catalog.llm_models import serialize_model_for_snapshot


def serialize_tool_for_snapshot(row: ToolDefinition) -> dict[str, Any]:
    """钉死工具元信息（含 MCP 连接 config），旧会话不随目录修改漂移。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "tool_type": row.tool_type,
        "class_path": row.class_path,
        "requires_hitl": bool(row.requires_hitl),
        "config": dict(row.config or {}),
        "status": row.status,
    }


def serialize_middleware_for_snapshot(row: MiddlewareDefinition) -> dict[str, Any]:
    """钉死中间件 class_path + 构造 config。"""
    return {
        "id": row.id,
        "name": row.name,
        "class_path": row.class_path,
        "config": dict(row.config or {}),
    }


def serialize_skill_for_live(row: SkillDefinition) -> dict[str, Any]:
    """live 组装用：保留完整 content。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "content": row.content,
        "config": dict(row.config or {}),
        "status": row.status,
    }


async def serialize_skill_for_snapshot(
    db: AsyncSession, row: SkillDefinition
) -> dict[str, Any]:
    """钉死 Skill 元信息；正文写入 content_blob，快照只存 hash。"""
    digest = await ensure_content_blob(db, row.content or "")
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "content_hash": digest,
        "config": dict(row.config or {}),
        "status": row.status,
    }


def serialize_agent_for_live(agent: AgentDefinition) -> dict[str, Any]:
    """live ORM → 组装用 dict（含明文正文）。"""
    return {
        "id": agent.id,
        "name": agent.name,
        "system_prompt": agent.system_prompt,
        "model_id": agent.model_id,
        "config": dict(agent.config or {}),
        "tools": [serialize_tool_for_snapshot(t) for t in agent.tools],
        "middlewares": [
            serialize_middleware_for_snapshot(m) for m in agent.middlewares
        ],
        "skills": [serialize_skill_for_live(s) for s in agent.skills],
        "llm": serialize_model_for_snapshot(agent.llm_model),
    }


async def serialize_agent_for_snapshot(
    db: AsyncSession, agent: AgentDefinition
) -> dict[str, Any]:
    """live Agent ORM → revision 快照 dict（正文以 hash 引用）。"""
    prompt_hash = await ensure_content_blob(db, agent.system_prompt or "")
    return {
        "id": agent.id,
        "name": agent.name,
        "system_prompt_hash": prompt_hash,
        "model_id": agent.model_id,
        "config": dict(agent.config or {}),
        "tools": [serialize_tool_for_snapshot(t) for t in agent.tools],
        "middlewares": [
            serialize_middleware_for_snapshot(m) for m in agent.middlewares
        ],
        "skills": [
            await serialize_skill_for_snapshot(db, s) for s in agent.skills
        ],
        "llm": serialize_model_for_snapshot(agent.llm_model),
    }
