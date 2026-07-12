"""
文档撰写子 Agent
================

专责：把用户需求变成结构清晰的 Markdown 文档，并落盘到 workspace/documents。
"""

from __future__ import annotations

from deepagents_app.middleware.logging_middleware import LoggingMiddleware
from deepagents_app.middleware.timing_middleware import TimingMiddleware
from deepagents_app.tools.document_tools import DOCUMENT_TOOLS

DOCUMENT_WRITER_PROMPT = """\
你是专业的**文档撰写 Agent**。

## 目标
根据主 Agent 下发的任务描述，产出结构完整、可读性强的 Markdown 文档。

## 工作流程
1. 若任务涉及已有文档，先用 `list_documents` / `read_document` 了解现状
2. 用 `create_document` 创建新文档，或用 `append_document_section` 增补章节
3. 必要时再次 `read_document` 自检
4. 向主 Agent 返回：文件名、路径、文档大纲、完成说明

## 写作标准
- 标题层级清晰（# / ## / ###）
- 先总后分；关键信息用列表或表格
- 避免空话；需要假设时明确写出假设
- 默认简体中文

## 边界
- 不要执行 shell 或系统操作
- 不要回答与文档无关的百科问题（应告知主 Agent 改派 qa-expert）
"""


def build_document_writer_subagent() -> dict:
    """返回 deepagents SubAgent 规格字典。"""
    return {
        "name": "document-writer",
        "description": (
            "文档撰写专家。适用于：撰写/改写 README、技术方案、会议纪要、"
            "报告、说明书等 Markdown 文档，并将结果保存到工作区。"
        ),
        "system_prompt": DOCUMENT_WRITER_PROMPT,
        "tools": list(DOCUMENT_TOOLS),
        # 子 Agent 也可挂自定义 middleware（与主 Agent 栈相互独立）
        "middleware": [
            LoggingMiddleware(),
            TimingMiddleware(),
        ],
    }
