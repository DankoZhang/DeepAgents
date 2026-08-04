"""
默认种子数据
============

幂等写入：
1. 默认大模型目录
2. 内置 Tool（builtin）/ Middleware
3. Skills（从 deepagents_app/skills 导入 SKILL.md）
4. 全局 demo Agents（含 tool / skill 绑定）
5. demo 方法论勾选上述 Agents 并发布
"""

from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from deepagents_app.config import PROJECT_ROOT, get_settings
from deepagents_app.db.models import AgentDefinition, Methodology, MiddlewareDefinition, ToolDefinition
from deepagents_app.services.agents import bind_agent_skills, create_agent
from deepagents_app.services.llm_models import ensure_default_model_from_settings
from deepagents_app.services.methodology import (
    create_methodology,
    publish_methodology,
)
from deepagents_app.services.middlewares import create_middleware
from deepagents_app.services.revisions import snapshot_methodology
from deepagents_app.services.skills import import_skill_from_file
from deepagents_app.services.tools import create_builtin_tool
from deepagents_app.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

PROMPTS_DIR = PROJECT_ROOT / "deepagents_app" / "config" / "prompts"
SKILLS_DIR = PROJECT_ROOT / "deepagents_app" / "skills"
DEFAULT_MODEL_ID = "model_default"

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
    },
    {
        "id": "tool_run_shell_command",
        "name": "run_shell_command",
        "description": "执行白名单 shell 命令",
        "class_path": "deepagents_app.tools.computer_tools:run_shell_command",
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

# 文件系统 skills → DB；id 固定便于 Agent 绑定
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

DEMO_METHODOLOGY_ID = "demo_deepagents"
DEMO_AGENT_IDS = [
    "agent_demo_supervisor",
    "agent_demo_document_writer",
    "agent_demo_computer_operator",
    "agent_demo_qa_expert",
]


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"你是 {name} 子 Agent。"


def seed_tools_and_middlewares(db: Session) -> None:
    """幂等（幂等性保证：如果工具或中间件已经存在，则不进行创建）写入内置 Tool / Middleware。"""
    for item in DEFAULT_TOOLS:
        if db.get(ToolDefinition, item["id"]) is None:
            create_builtin_tool(
                db,
                tool_id=item["id"],
                name=item["name"],
                description=item["description"],
                class_path=item["class_path"],
            )
            logger.info("种子工具：%s", item["name"])
    for item in DEFAULT_MIDDLEWARES:
        if db.get(MiddlewareDefinition, item["id"]) is None:
            create_middleware(
                db,
                middleware_id=item["id"],
                name=item["name"],
                class_path=item["class_path"],
            )
            logger.info("种子中间件：%s", item["name"])


def seed_skills(db: Session) -> None:
    """从项目 skills/ 目录幂等导入 SKILL.md 到 skill_definition。"""
    for item in DEFAULT_SKILLS:
        row = import_skill_from_file(db, item["path"], skill_id=item["id"])
        if row is not None:
            logger.info("种子 Skill：%s (%s)", row.name, row.id)


def seed_demo_agents(db: Session) -> None:
    """幂等写入全局 demo Agents（已存在则跳过创建；可回填默认 model_id / skills）。"""
    doc_tools = [
        "tool_create_document",
        "tool_append_document_section",
        "tool_list_documents",
        "tool_read_document",
    ]
    computer_tools = [
        "tool_list_workspace",
        "tool_read_workspace_file",
        "tool_write_workspace_file",
        "tool_run_shell_command",
    ]
    qa_tools = [
        "tool_search_knowledge",
        "tool_list_knowledge_topics",
        "tool_save_qa_note",
    ]

    specs = [
        {
            "agent_id": "agent_demo_supervisor",
            "name": "supervisor",
            "system_prompt": SUPERVISOR_SYSTEM_PROMPT,
            "config": {
                "role": "supervisor",
                "enabled": True,
                "description": "主调度 Agent",
            },
            "tool_ids": [],
            "middleware_ids": ["mw_logging", "mw_timing", "mw_audit"],
            "skill_ids": [],
        },
        {
            "agent_id": "agent_demo_document_writer",
            "name": "document-writer",
            "system_prompt": _read_prompt("document-writer.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
                "description": "文档撰写专家。适用于撰写/改写 Markdown 文档。",
            },
            "tool_ids": doc_tools,
            "middleware_ids": ["mw_logging", "mw_timing"],
            "skill_ids": ["skill_document_writing"],
        },
        {
            "agent_id": "agent_demo_computer_operator",
            "name": "computer-operator",
            "system_prompt": _read_prompt("computer-operator.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
                "description": "计算机操作专家。适用于浏览 workspace、执行白名单 shell。",
            },
            "tool_ids": computer_tools,
            "middleware_ids": ["mw_logging", "mw_timing", "mw_audit"],
            "skill_ids": ["skill_computer_ops"],
        },
        {
            "agent_id": "agent_demo_qa_expert",
            "name": "qa-expert",
            "system_prompt": _read_prompt("qa-expert.md"),
            "config": {
                "role": "subagent",
                "enabled": True,
                "description": "智能问答专家。适用于概念解释与知识库检索。",
            },
            "tool_ids": qa_tools,
            "middleware_ids": ["mw_logging"],
            "skill_ids": ["skill_qa_answering"],
        },
    ]

    for spec in specs:
        existing = db.get(AgentDefinition, spec["agent_id"])
        if existing is not None:
            # 旧库升级：补绑默认模型，不 bump（避免启动时无谓升版）
            if existing.model_id is None:
                existing.model_id = DEFAULT_MODEL_ID
                logger.info("回填 Agent 默认模型：%s", existing.name)
            # 清掉遗留 config.skills 路径，改走 agent_skill
            cfg = dict(existing.config or {})
            if "skills" in cfg:
                cfg.pop("skills", None)
                existing.config = cfg
                logger.info("清除 Agent 遗留 config.skills：%s", existing.name)
            if spec["skill_ids"] and not existing.skills:
                bind_agent_skills(
                    db,
                    existing.id,
                    spec["skill_ids"],
                    replace=True,
                    bump_related=False,
                )
                logger.info("回填 Agent Skills：%s -> %s", existing.name, spec["skill_ids"])
            continue
        create_agent(
            db,
            agent_id=spec["agent_id"],
            name=spec["name"],
            system_prompt=spec["system_prompt"],
            model_id=DEFAULT_MODEL_ID,
            config=spec["config"],
            tool_ids=spec["tool_ids"],
            middleware_ids=spec["middleware_ids"],
            skill_ids=spec["skill_ids"],
            bump_related=False,
        )
        logger.info("种子 Agent：%s", spec["name"])


def seed_demo_methodology(db: Session) -> None:
    """幂等（幂等性保证：如果方法论已经存在，则不进行创建）写入演示方法论并勾选全局 demo Agents。"""
    if db.get(Methodology, DEMO_METHODOLOGY_ID) is not None:
        return

    create_methodology(
        db,
        name="DeepAgents 演示方法论",
        description="Supervisor + document-writer / computer-operator / qa-expert",
        methodology_id=DEMO_METHODOLOGY_ID,
        agent_ids=DEMO_AGENT_IDS,
    )
    publish_methodology(db, DEMO_METHODOLOGY_ID)
    snapshot_methodology(db, DEMO_METHODOLOGY_ID)
    logger.info("已种子化演示方法论：%s", DEMO_METHODOLOGY_ID)


def seed_defaults(db: Session) -> None:
    """应用启动入口：默认模型 → 工具/中间件 → Skills → 全局 Agents → 演示方法论。"""
    ensure_default_model_from_settings(db, get_settings(), model_id=DEFAULT_MODEL_ID)
    seed_tools_and_middlewares(db)
    seed_skills(db)
    seed_demo_agents(db)
    seed_demo_methodology(db)
