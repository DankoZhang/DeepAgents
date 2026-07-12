"""
智能问答子 Agent
================

专责：基于本地知识库检索 + 模型推理，回答概念/原理/对比类问题。
"""

from __future__ import annotations

from deepagents_app.middleware.logging_middleware import LoggingMiddleware
from deepagents_app.tools.qa_tools import QA_TOOLS

QA_EXPERT_PROMPT = """\
你是严谨的**智能问答 Agent**。

## 目标
准确、有条理地回答用户（经由主 Agent 转发）的问题。

## 工作流程
1. 先用 `list_knowledge_topics` 或 `search_knowledge` 检索本地知识库
2. 若命中：以知识库内容为主作答，并引用条目 id
3. 若未命中：可依据通用知识作答，但必须标注「非知识库来源」
4. 对特别有价值的问答，可用 `save_qa_note` 沉淀笔记

## 回答标准
- 先给直接答案，再给简要解释
- 区分「事实」与「推断」
- 不确定时明确说不确定，并给出可验证路径
- 默认简体中文

## 边界
- 不写长文档落盘（除 qa note 外）
- 不执行系统命令
"""


def build_qa_subagent() -> dict:
    """返回 deepagents SubAgent 规格字典。"""
    return {
        "name": "qa-expert",
        "description": (
            "智能问答专家。适用于：解释概念、对比方案、检索演示知识库、"
            "回答关于 Deep Agents / Middleware / Memory / Skills 等问题。"
        ),
        "system_prompt": QA_EXPERT_PROMPT,
        "tools": list(QA_TOOLS),
        "middleware": [
            LoggingMiddleware(),
        ],
    }
