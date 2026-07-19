"""
默认种子数据
============

幂等写入：
1. 内置 Tool / Middleware 元信息
2. demo 方法论（Supervisor + 三个 SubAgent），对齐原 YAML 演示配置
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy.orm import Session

from deepagents_app.config import PROJECT_ROOT
from deepagents_app.db.models import Methodology, MiddlewareDefinition, ToolDefinition
from deepagents_app.services.agents import create_agent
from deepagents_app.services.methodology import create_methodology, publish_methodology
from deepagents_app.services.middlewares import create_middleware
from deepagents_app.services.revisions import snapshot_methodology
from deepagents_app.services.tools import create_tool
from deepagents_app.supervisor.prompts import SUPERVISOR_SYSTEM_PROMPT

logger = logging.getLogger(__name__)

PROMPTS_DIR = PROJECT_ROOT / "deepagents_app" / "config" / "prompts"

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

DEMO_METHODOLOGY_ID = "demo_deepagents"


def _read_prompt(name: str) -> str:
    path = PROMPTS_DIR / name
    if path.exists():
        return path.read_text(encoding="utf-8")
    return f"你是 {name} 子 Agent。"


def seed_tools_and_middlewares(db: Session) -> None:
    for item in DEFAULT_TOOLS:
        if db.get(ToolDefinition, item["id"]) is None:
            create_tool(
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


def seed_demo_methodology(db: Session) -> None:
    if db.get(Methodology, DEMO_METHODOLOGY_ID) is not None:
        return

    create_methodology(
        db,
        name="DeepAgents 演示方法论",
        description="Supervisor + document-writer / computer-operator / qa-expert",
        methodology_id=DEMO_METHODOLOGY_ID,
    )

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

    create_agent(
        db,
        methodology_id=DEMO_METHODOLOGY_ID,
        agent_id="agent_demo_supervisor",
        name="supervisor",
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        config={"role": "supervisor", "enabled": True, "description": "主调度 Agent"},
        middleware_ids=["mw_logging", "mw_timing", "mw_audit"],
        bump_version=False,
    )
    create_agent(
        db,
        methodology_id=DEMO_METHODOLOGY_ID,
        agent_id="agent_demo_document_writer",
        name="document-writer",
        system_prompt=_read_prompt("document-writer.md"),
        config={
            "role": "subagent",
            "enabled": True,
            "description": "文档撰写专家。适用于撰写/改写 Markdown 文档。",
            "skills": ["/skills/document-writer/"],
        },
        tool_ids=doc_tools,
        middleware_ids=["mw_logging", "mw_timing"],
        bump_version=False,
    )
    create_agent(
        db,
        methodology_id=DEMO_METHODOLOGY_ID,
        agent_id="agent_demo_computer_operator",
        name="computer-operator",
        system_prompt=_read_prompt("computer-operator.md"),
        config={
            "role": "subagent",
            "enabled": True,
            "description": "计算机操作专家。适用于浏览 workspace、执行白名单 shell。",
            "skills": ["/skills/computer-operator/"],
        },
        tool_ids=computer_tools,
        middleware_ids=["mw_logging", "mw_timing", "mw_audit"],
        bump_version=False,
    )
    create_agent(
        db,
        methodology_id=DEMO_METHODOLOGY_ID,
        agent_id="agent_demo_qa_expert",
        name="qa-expert",
        system_prompt=_read_prompt("qa-expert.md"),
        config={
            "role": "subagent",
            "enabled": True,
            "description": "智能问答专家。适用于概念解释与知识库检索。",
            "skills": ["/skills/qa-expert/"],
        },
        tool_ids=qa_tools,
        middleware_ids=["mw_logging"],
        bump_version=False,
    )

    publish_methodology(db, DEMO_METHODOLOGY_ID)
    snapshot_methodology(db, DEMO_METHODOLOGY_ID)
    logger.info("已种子化演示方法论：%s", DEMO_METHODOLOGY_ID)


def seed_defaults(db: Session) -> None:
    seed_tools_and_middlewares(db)
    seed_demo_methodology(db)
