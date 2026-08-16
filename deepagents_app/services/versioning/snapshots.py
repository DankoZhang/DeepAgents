#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   snapshots.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   snapshots.py

快照序列化
==========

live ORM → 可持久化 JSON 的唯一入口。
``revisions``（落库）与 ``agent_factory``（组装归一）共用，避免两处漂移。

落库快照中 Skill / system_prompt / Memory 正文只存 content_blob 哈希引用；
组装前由 ``hydrate_snapshot_content`` 按 hash 还原正文。
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
from deepagents_app.services.catalog.llm_models import serialize_model
from deepagents_app.utils.skill_package import skill_files_map


def serialize_tool(row: ToolDefinition) -> dict[str, Any]:
    """工具元信息（含 MCP 连接 config）；live 组装与快照共用。"""
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


def serialize_middleware(row: MiddlewareDefinition) -> dict[str, Any]:
    """中间件 class_path + 构造 config；live 组装与快照共用。"""
    return {
        "id": row.id,
        "name": row.name,
        "class_path": row.class_path,
        "config": dict(row.config or {}),
    }


def _skill_common_fields(row: SkillDefinition) -> dict[str, Any]:
    """Skill live / snapshot 共有字段（正文除外）。"""
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "config": dict(row.config or {}),
        "status": row.status,
    }


def serialize_skill_for_live(row: SkillDefinition) -> dict[str, Any]:
    """live 组装用：保留完整 content 与附属文件正文。"""
    return {
        **_skill_common_fields(row),
        "content": row.content,
        "files": skill_files_map(row.files),
    }


async def serialize_skill_for_snapshot(
    db: AsyncSession, row: SkillDefinition
) -> dict[str, Any]:
    """钉死 Skill 元信息；SKILL.md 与附属文件写入 content_blob，快照只存 hash。"""
    digest = await ensure_content_blob(db, row.content or "")
    file_entries: list[dict[str, str]] = []
    for path, body in skill_files_map(row.files).items():
        file_entries.append(
            {"path": path, "content_hash": await ensure_content_blob(db, body)}
        )
    return {
        **_skill_common_fields(row),
        "content_hash": digest,
        "files": file_entries,
    }


def _agent_common_fields(agent: AgentDefinition) -> dict[str, Any]:
    """Agent live / snapshot 共有字段（system_prompt / skills 除外）。"""
    return {
        "id": agent.id,
        "name": agent.name,
        "model_id": agent.model_id,
        "config": dict(agent.config or {}),
        "tools": [serialize_tool(t) for t in agent.tools],
        "middlewares": [serialize_middleware(m) for m in agent.middlewares],
        "llm": serialize_model(agent.llm_model),
    }


def serialize_agent_for_live(agent: AgentDefinition) -> dict[str, Any]:
    """live ORM → 组装用 dict（含明文正文）。"""
    return {
        **_agent_common_fields(agent),
        "system_prompt": agent.system_prompt,
        "skills": [serialize_skill_for_live(s) for s in agent.skills],
    }


async def serialize_agent_for_snapshot(
    db: AsyncSession, agent: AgentDefinition
) -> dict[str, Any]:
    """live Agent ORM → revision 快照 dict（正文以 hash 引用）。"""
    prompt_hash = await ensure_content_blob(db, agent.system_prompt or "")
    return {
        **_agent_common_fields(agent),
        "system_prompt_hash": prompt_hash,
        "skills": [
            await serialize_skill_for_snapshot(db, s) for s in agent.skills
        ],
    }
