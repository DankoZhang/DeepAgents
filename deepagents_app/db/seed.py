"""
默认种子数据（按用户）
====================

全局启动不再灌业务配置。用户首次鉴权通过后调用 ``ensure_user_bootstrap``，
幂等写入该用户的默认模型 / 内置 Tool / Middleware / Skills / demo Agents / 方法论。
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from deepagents_app.config import PROJECT_ROOT, get_settings
from deepagents_app.db.models import AgentDefinition, Methodology, MiddlewareDefinition, ToolDefinition
from deepagents_app.ownership import demo_methodology_id_for_user, scoped_id
from deepagents_app.services.agents import create_agent
from deepagents_app.services.llm_models import ensure_default_model_from_settings
from deepagents_app.services.methodology import (
    create_methodology,
    publish_methodology,
)
from deepagents_app.services.middlewares import create_middleware
from deepagents_app.services.skills import import_skill_from_file
from deepagents_app.services.tools import create_builtin_tool
from deepagents_app.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

PROMPTS_DIR = PROJECT_ROOT / "deepagents_app" / "prompts"
SKILLS_DIR = PROJECT_ROOT / "deepagents_app" / "skills"

DEFAULT_TOOLS: list[dict] = [
    {
        "id": "tool_create_document",
        "name": "create_document",
        "description": "创建 Markdown 文档",
        "class_path": "deepagents_app.tools.document_tools:create_document",
    },
    {
        "id": "tool_append_document_section",
        "name": "append_document_section",
        "description": "向文档追加章节",
        "class_path": "deepagents_app.tools.document_tools:append_document_section",
    },
    {
        "id": "tool_list_documents",
        "name": "list_documents",
        "description": "列出文档",
        "class_path": "deepagents_app.tools.document_tools:list_documents",
    },
    {
        "id": "tool_read_document",
        "name": "read_document",
        "description": "读取文档",
        "class_path": "deepagents_app.tools.document_tools:read_document",
    },
    {
        "id": "tool_list_workspace",
        "name": "list_workspace",
        "description": "列出工作区目录",
        "class_path": "deepagents_app.tools.computer_tools:list_workspace",
    },
    {
        "id": "tool_read_workspace_file",
        "name": "read_workspace_file",
        "description": "读取工作区文件",
        "class_path": "deepagents_app.tools.computer_tools:read_workspace_file",
    },
    {
        "id": "tool_write_workspace_file",
        "name": "write_workspace_file",
        "description": "写入工作区文件",
        "class_path": "deepagents_app.tools.computer_tools:write_workspace_file",
        "requires_hitl": True,
    },
    {
        "id": "tool_run_shell_command",
        "name": "run_shell_command",
        "description": "执行白名单 shell 命令",
        "class_path": "deepagents_app.tools.computer_tools:run_shell_command",
        "requires_hitl": True,
    },
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
        "id": "skill_document_writing",
        "path": SKILLS_DIR / "document-writer" / "document-writing" / "SKILL.md",
    },
    {
        "id": "skill_computer_ops",
        "path": SKILLS_DIR / "computer-operator" / "computer-ops" / "SKILL.md",
    },
    {
        "id": "skill_qa_answering",
        "path": SKILLS_DIR / "qa-expert" / "qa-answering" / "SKILL.md",
    },
]

DEMO_AGENT_BASE_IDS = [
    "agent_demo_supervisor",
    "agent_demo_document_writer",
    "agent_demo_computer_operator",
    "agent_demo_qa_expert",
]

_bootstrapped_users: set[str] = set()


def _sid(owner_user_id: str, base_id: str) -> str:
    return scoped_id(owner_user_id, base_id)


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"你是 {name} 子 Agent。"


def seed_tools_and_middlewares(db: Session, *, owner_user_id: str) -> None:
    for item in DEFAULT_TOOLS:
        tool_id = _sid(owner_user_id, item["id"])
        if db.get(ToolDefinition, tool_id) is None:
            create_builtin_tool(
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
        if db.get(MiddlewareDefinition, mw_id) is None:
            create_middleware(
                db,
                owner_user_id=owner_user_id,
                middleware_id=mw_id,
                name=item["name"],
                class_path=item["class_path"],
            )
            logger.info("种子中间件[%s]：%s", owner_user_id, item["name"])


def seed_skills(db: Session, *, owner_user_id: str) -> None:
    for item in DEFAULT_SKILLS:
        row = import_skill_from_file(
            db,
            item["path"],
            owner_user_id=owner_user_id,
            skill_id=_sid(owner_user_id, item["id"]),
        )
        if row is not None:
            logger.info("种子 Skill[%s]：%s (%s)", owner_user_id, row.name, row.id)


def seed_demo_agents(db: Session, *, owner_user_id: str) -> None:
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
                "enabled": True,
                "description": "主调度 Agent",
            },
            "tool_ids": [],
            "middleware_ids": mws("mw_logging", "mw_timing", "mw_audit"),
            "skill_ids": [],
        },
        {
            "agent_id": _sid(owner_user_id, "agent_demo_document_writer"),
            "name": "document-writer",
            "system_prompt": _read_prompt("document-writer.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
                "description": "文档撰写专家。适用于撰写/改写 Markdown 文档。",
            },
            "tool_ids": tools(
                "tool_create_document",
                "tool_append_document_section",
                "tool_list_documents",
                "tool_read_document",
            ),
            "middleware_ids": mws("mw_logging", "mw_timing"),
            "skill_ids": skills("skill_document_writing"),
        },
        {
            "agent_id": _sid(owner_user_id, "agent_demo_computer_operator"),
            "name": "computer-operator",
            "system_prompt": _read_prompt("computer-operator.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
                "description": "计算机操作专家。适用于浏览 workspace、执行白名单 shell。",
            },
            "tool_ids": tools(
                "tool_list_workspace",
                "tool_read_workspace_file",
                "tool_write_workspace_file",
                "tool_run_shell_command",
            ),
            "middleware_ids": mws("mw_logging", "mw_timing", "mw_audit"),
            "skill_ids": skills("skill_computer_ops"),
        },
        {
            "agent_id": _sid(owner_user_id, "agent_demo_qa_expert"),
            "name": "qa-expert",
            "system_prompt": _read_prompt("qa-expert.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
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
        if db.get(AgentDefinition, spec["agent_id"]) is not None:
            continue
        create_agent(
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


def seed_demo_methodology(db: Session, *, owner_user_id: str) -> str:
    methodology_id = demo_methodology_id_for_user(owner_user_id)
    if db.get(Methodology, methodology_id) is not None:
        return methodology_id

    agent_ids = [_sid(owner_user_id, base) for base in DEMO_AGENT_BASE_IDS]
    create_methodology(
        db,
        owner_user_id=owner_user_id,
        name="DeepAgents 演示方法论",
        description="Supervisor + document-writer / computer-operator / qa-expert",
        methodology_id=methodology_id,
        agent_ids=agent_ids,
    )
    publish_methodology(db, methodology_id, owner_user_id=owner_user_id)
    logger.info("已种子化演示方法论[%s]：%s", owner_user_id, methodology_id)
    return methodology_id


def ensure_user_bootstrap(db: Session, owner_user_id: str) -> None:
    """幂等：为当前用户准备默认配置（进程内短缓存，避免每请求重复查）。"""
    if owner_user_id in _bootstrapped_users:
        # 仍以方法论是否存在为准，防止进程缓存跨库测试脏读
        if db.get(Methodology, demo_methodology_id_for_user(owner_user_id)) is not None:
            return

    ensure_default_model_from_settings(db, get_settings(), owner_user_id=owner_user_id)
    seed_tools_and_middlewares(db, owner_user_id=owner_user_id)
    seed_skills(db, owner_user_id=owner_user_id)
    seed_demo_agents(db, owner_user_id=owner_user_id)
    seed_demo_methodology(db, owner_user_id=owner_user_id)
    _bootstrapped_users.add(owner_user_id)


def clear_bootstrap_cache() -> None:
    _bootstrapped_users.clear()
