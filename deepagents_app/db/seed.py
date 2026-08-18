#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   seed.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   seed.py

默认种子数据（按用户）
====================

用户通过 ``ensure_user_bootstrap`` 幂等写入该用户的默认模型 /
内置 Tool / Middleware / Skills / demo Agents / 方法论。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.config import PROJECT_ROOT, get_settings
from deepagents_app.db.models import AgentDefinition, Methodology, MiddlewareDefinition, ToolDefinition
from deepagents_app.ownership import demo_methodology_id_for_user, scoped_id
from deepagents_app.services.catalog.agents import create_agent, enable_agent
from deepagents_app.services.catalog.llm_models import ensure_default_model_from_settings
from deepagents_app.services.catalog.middlewares import create_middleware
from deepagents_app.services.catalog.skills import import_skill_from_path
from deepagents_app.services.catalog.tools import create_builtin_tool
from deepagents_app.workspace import user_workspace_dir
from deepagents_app.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

PROMPTS_DIR = PROJECT_ROOT / "deepagents_app" / "prompts"
SKILLS_DIR = PROJECT_ROOT / "deepagents_app" / "skills"

DEFAULT_TOOLS: list[dict] = [
    {
        "id": "tool_search_knowledge",
        "name": "search_knowledge",
        "description": "检索演示知识库",
        "class_path": "deepagents_app.tools.qa_tools:search_knowledge",
    },
    {
        "id": "tool_list_knowledge_topics",
        "name": "list_knowledge_topics",
        "description": "列出知识库主题",
        "class_path": "deepagents_app.tools.qa_tools:list_knowledge_topics",
    },
    {
        "id": "tool_save_qa_note",
        "name": "save_qa_note",
        "description": "保存问答笔记",
        "class_path": "deepagents_app.tools.qa_tools:save_qa_note",
    },
]

DEFAULT_MIDDLEWARES: list[dict] = [
    {
        "id": "mw_logging",
        "name": "LoggingMiddleware",
        "class_path": "deepagents_app.middleware.logging_middleware:LoggingMiddleware",
    },
    {
        "id": "mw_timing",
        "name": "TimingMiddleware",
        "class_path": "deepagents_app.middleware.timing_middleware:TimingMiddleware",
    },
    {
        "id": "mw_audit",
        "name": "AuditMiddleware",
        "class_path": "deepagents_app.middleware.audit_middleware:AuditMiddleware",
    },
]

DEFAULT_SKILLS: list[dict] = [
    {
        "id": "skill_qa_answering",
        "path": SKILLS_DIR / "qa-expert" / "qa-answering" / "SKILL.md",
    },
]

# 每个 worker 进程各一份：只加速本进程后续请求，不跨进程共享
_bootstrapped_users: set[str] = set()
_bootstrap_locks: dict[str, asyncio.Lock] = {}


def _sid(owner_user_id: str, base_id: str) -> str:
    """种子资源主键：逻辑 id + 用户 scope，避免跨用户撞号。"""
    return scoped_id(owner_user_id, base_id)


def _user_bootstrap_lock(owner_user_id: str) -> asyncio.Lock:
    """同进程内按用户串行 bootstrap（asyncio.Lock，避免包住 await 时挂死事件循环）。"""
    lock = _bootstrap_locks.get(owner_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _bootstrap_locks[owner_user_id] = lock
    return lock


async def _pg_advisory_xact_lock(db: AsyncSession, owner_user_id: str) -> None:
    """
    多 worker 下用事务级劝告锁串行同一用户的 bootstrap。

    非 PostgreSQL（如 SQLite 测试）直接跳过。锁 key 由
    ``deepagents:bootstrap:{user}`` 的 sha256 前 8 字节拆成两个有符号
    int32，满足 ``pg_advisory_xact_lock(int, int)``；事务结束自动释放。
    """
    bind = db.bind
    if bind is None or bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(f"deepagents:bootstrap:{owner_user_id}".encode()).digest()
    # PG integer 有符号；无符号 0x80000000+ 必须按 signed 传入
    k1 = int.from_bytes(digest[0:4], "big", signed=True)
    k2 = int.from_bytes(digest[4:8], "big", signed=True)
    await db.execute(
        text("SELECT pg_advisory_xact_lock(:k1, :k2)"),
        {"k1": k1, "k2": k2},
    )


def _read_prompt(name: str) -> str:
    """读取 ``prompts/`` 下的子 Agent 提示词；文件缺失时给占位句。"""
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"你是 {name} 子 Agent。"


async def seed_tools_and_middlewares(db: AsyncSession, *, owner_user_id: str) -> None:
    """按用户写入内置 Tool / Middleware；已存在则跳过。"""
    for item in DEFAULT_TOOLS:
        tool_id = _sid(owner_user_id, item["id"])
        if await db.get(ToolDefinition, tool_id) is None:
            await create_builtin_tool(
                db,
                owner_user_id=owner_user_id,
                tool_id=tool_id,
                name=item["name"],
                description=item["description"],
                class_path=item["class_path"],
                requires_hitl=bool(item.get("requires_hitl", False)),
            )
            logger.info("种子工具[%s]：%s", owner_user_id, item["name"])
    for item in DEFAULT_MIDDLEWARES:
        mw_id = _sid(owner_user_id, item["id"])
        if await db.get(MiddlewareDefinition, mw_id) is None:
            await create_middleware(
                db,
                owner_user_id=owner_user_id,
                middleware_id=mw_id,
                name=item["name"],
                class_path=item["class_path"],
            )
            logger.info("种子中间件[%s]：%s", owner_user_id, item["name"])


async def seed_skills(db: AsyncSession, *, owner_user_id: str) -> None:
    """从仓库 ``skills/`` 导入演示 Skill；已存在则跳过。"""
    for item in DEFAULT_SKILLS:
        row = await import_skill_from_path(
            db,
            item["path"],
            owner_user_id=owner_user_id,
            skill_id=_sid(owner_user_id, item["id"]),
        )
        if row is not None:
            logger.info("种子 Skill[%s]：%s (%s)", owner_user_id, row.name, row.id)


async def seed_demo_agents(db: AsyncSession, *, owner_user_id: str) -> None:
    """写入演示主 Agent / qa-expert；默认未启用，随后由 enable 发布方法论。"""
    def tools(*base_ids: str) -> list[str]:
        return [_sid(owner_user_id, i) for i in base_ids]

    def mws(*base_ids: str) -> list[str]:
        return [_sid(owner_user_id, i) for i in base_ids]

    def skills(*base_ids: str) -> list[str]:
        return [_sid(owner_user_id, i) for i in base_ids]

    specs = [
        {
            "agent_id": _sid(owner_user_id, "agent_demo_supervisor"),
            "name": "supervisor",
            "system_prompt": SUPERVISOR_SYSTEM_PROMPT,
            "config": {
                "role": "supervisor",
                "description": "主调度 Agent",
                "subagent_ids": [_sid(owner_user_id, "agent_demo_qa_expert")],
                "methodology_id": demo_methodology_id_for_user(owner_user_id),
            },
            "tool_ids": [],
            "middleware_ids": mws("mw_logging", "mw_timing", "mw_audit"),
            "skill_ids": [],
        },
        {
            "agent_id": _sid(owner_user_id, "agent_demo_qa_expert"),
            "name": "qa-expert",
            "system_prompt": _read_prompt("qa-expert.md"),
            "config": {
                "role": "subagent",
                "description": "智能问答专家。适用于概念解释与知识库检索。",
            },
            "tool_ids": tools(
                "tool_search_knowledge",
                "tool_list_knowledge_topics",
                "tool_save_qa_note",
            ),
            "middleware_ids": mws("mw_logging"),
            "skill_ids": skills("skill_qa_answering"),
        },
    ]

    for spec in specs:
        if await db.get(AgentDefinition, spec["agent_id"]) is not None:
            continue
        await create_agent(
            db,
            owner_user_id=owner_user_id,
            agent_id=spec["agent_id"],
            name=spec["name"],
            system_prompt=spec["system_prompt"],
            model_id=None,
            config=spec["config"],
            tool_ids=spec["tool_ids"],
            middleware_ids=spec["middleware_ids"],
            skill_ids=spec["skill_ids"],
            bump_related=False,
        )
        logger.info("种子 Agent[%s]：%s", owner_user_id, spec["name"])


async def _align_demo_supervisor(
    db: AsyncSession, owner_user_id: str, methodology_id: str
) -> None:
    """
    演示方法论已存在时，补齐主 Agent 与产品门面字段。

    旧库可能缺 ``methodology_id`` / ``subagent_ids``；若方法论已 published，
    把 supervisor 名称和方法论对齐，并打开两边的 ``enabled``。
    """
    methodology = await db.get(Methodology, methodology_id)
    if methodology is None or methodology.owner_user_id != owner_user_id:
        return
    supervisor_id = _sid(owner_user_id, "agent_demo_supervisor")
    qa_id = _sid(owner_user_id, "agent_demo_qa_expert")
    supervisor = await db.get(AgentDefinition, supervisor_id)
    if supervisor is None:
        return
    cfg = dict(supervisor.config or {})
    cfg.setdefault("methodology_id", methodology_id)
    cfg.setdefault("subagent_ids", [qa_id])
    if methodology.status == "published":
        cfg["enabled"] = True
        if methodology.name != supervisor.name:
            methodology.name = supervisor.name
    supervisor.config = cfg
    qa = await db.get(AgentDefinition, qa_id)
    if qa is not None and methodology.status == "published":
        qa_cfg = dict(qa.config or {})
        qa_cfg["enabled"] = True
        qa.config = qa_cfg
    await db.flush()


async def seed_demo_methodology(db: AsyncSession, *, owner_user_id: str) -> str:
    """
    走与产品相同的 enable 门面发布演示方法论。

    已有该方法论时只 align，不再二次 enable。先启用子 Agent，再启用
    supervisor，发布时成员才会包含 qa-expert。
    """
    methodology_id = demo_methodology_id_for_user(owner_user_id)
    if await db.get(Methodology, methodology_id) is not None:
        await _align_demo_supervisor(db, owner_user_id, methodology_id)
        return methodology_id

    qa_id = _sid(owner_user_id, "agent_demo_qa_expert")
    supervisor_id = _sid(owner_user_id, "agent_demo_supervisor")
    await enable_agent(db, qa_id, owner_user_id=owner_user_id)
    await enable_agent(db, supervisor_id, owner_user_id=owner_user_id)
    logger.info("已种子化演示方法论[%s]：%s", owner_user_id, methodology_id)
    return methodology_id


async def ensure_user_bootstrap(db: AsyncSession, owner_user_id: str) -> None:
    """
    幂等：为当前用户准备默认配置。

    - 进程内短缓存 + 按用户锁：同进程并发安全
    - PostgreSQL advisory lock：多 worker 并发安全
    - IntegrityError + 再读：兜底竞态（如 SQLite 测试）
    """
    demo_id = demo_methodology_id_for_user(owner_user_id)
    async with _user_bootstrap_lock(owner_user_id):
        # 本进程已做过：再确认库里还有演示方法论（防止被删后缓存撒谎）
        if owner_user_id in _bootstrapped_users:
            if await db.get(Methodology, demo_id) is not None:
                return

        # 先锁后查，避免两个 worker 都看见「没有」再各自去种
        await _pg_advisory_xact_lock(db, owner_user_id)
        if await db.get(Methodology, demo_id) is not None:
            await _align_demo_supervisor(db, owner_user_id, demo_id)
            _bootstrapped_users.add(owner_user_id)
            return

        try:
            async with db.begin_nested():
                await ensure_default_model_from_settings(
                    db, get_settings(), owner_user_id=owner_user_id
                )
                await seed_tools_and_middlewares(db, owner_user_id=owner_user_id)
                await seed_skills(db, owner_user_id=owner_user_id)
                await seed_demo_agents(db, owner_user_id=owner_user_id)
                await seed_demo_methodology(db, owner_user_id=owner_user_id)
                # 工作区目录只在 bootstrap / 组装 miss 时创建，聊天热路径不再 mkdir
                user_workspace_dir(get_settings(), owner_user_id, ensure=True)
        except IntegrityError:
            logger.warning(
                "bootstrap 遇唯一约束冲突，按已存在数据处理：%s", owner_user_id
            )
            if await db.get(Methodology, demo_id) is None:
                raise
        _bootstrapped_users.add(owner_user_id)


def clear_bootstrap_cache() -> None:
    """测试用：清空进程内已 bootstrap 用户集合。"""
    _bootstrapped_users.clear()
