"""
智能问答工具
============

供 ``qa-expert`` 子 Agent 使用。

本演示用**本地知识库**模拟检索增强（RAG）：
- ``knowledge_base``：内置若干条目
- ``search_knowledge``：关键词检索
- ``save_qa_note``：把高质量问答沉淀到 workspace/notes

真实项目可替换为向量库 / 搜索引擎 / 企业内部 Wiki API。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from deepagents_app.config import get_settings

# ---------------------------------------------------------------------------
# 演示用迷你知识库（生产环境请换成外部数据源）
# ---------------------------------------------------------------------------
KNOWLEDGE_BASE: list[dict[str, str]] = [
    {
        "id": "kb-001",
        "title": "什么是 Deep Agents",
        "tags": "deepagents langchain agent harness",
        "content": (
            "Deep Agents 是 LangChain 提供的 Agent Harness："
            "在标准 tool-calling 循环之上，内置任务规划（write_todos）、"
            "虚拟文件系统、子 Agent 委派（task）、上下文摘要、Memory/Skills、"
            "以及 Human-in-the-loop 等能力，适合多步骤复杂任务。"
        ),
    },
    {
        "id": "kb-002",
        "title": "主 Agent 与子 Agent 的分工",
        "tags": "supervisor subagent task 调度",
        "content": (
            "主 Agent（Supervisor）负责理解意图、拆解任务、调用 task 工具委派；"
            "子 Agent 在隔离上下文中完成专业子任务，只把最终结果回传给主 Agent。"
            "这样可避免主 Agent 上下文被长文档或大量工具输出污染。"
        ),
    },
    {
        "id": "kb-003",
        "title": "Middleware 是什么",
        "tags": "middleware 中间件 hook",
        "content": (
            "Middleware 挂在 Agent 循环的关键钩子上（before/after model、"
            "wrap_model_call、wrap_tool_call 等），用于日志、审计、摘要、"
            "权限、动态改写 prompt、拦截危险工具等横切关注点。"
            "deepagents 默认栈已包含 TodoList / Filesystem / Summarization /"
            "SubAgent 等中间件；自定义 middleware 可通过 create_deep_agent 的"
            "middleware= 参数合并进去。"
        ),
    },
    {
        "id": "kb-004",
        "title": "Memory 与 Skills 的区别",
        "tags": "memory skills AGENTS.md SKILL.md",
        "content": (
            "Memory（如 AGENTS.md）在启动时完整加载，适合长期行为准则与偏好；"
            "Skills 采用渐进披露：启动时只读 frontmatter 索引，需要时再 load 全文，"
            "适合较重的领域工作流与参考资料。"
        ),
    },
    {
        "id": "kb-005",
        "title": "Human-in-the-loop",
        "tags": "hitl interrupt_on 人工审批",
        "content": (
            "通过 create_deep_agent(interrupt_on={...}) 可在指定工具调用前暂停，"
            "等待人工批准、修改参数或拒绝。常用于写文件、执行 shell、调用付费 API 等。"
        ),
    },
]


def _notes_dir() -> Path:
    return get_settings().workspace_dir / "notes"


class SearchKnowledgeArgs(BaseModel):
    query: str = Field(description="查询字符串（支持空格分词）")
    top_k: int = Field(default=3, ge=1, le=20, description="返回条数上限，默认 3")


class SaveQaNoteArgs(BaseModel):
    question: str = Field(description="用户问题")
    answer: str = Field(description="最终答案")
    tags: str = Field(default="", description="可选标签，逗号分隔")


@tool(args_schema=SearchKnowledgeArgs)
def search_knowledge(query: str, top_k: int = 3) -> str:
    """在本地知识库中按关键词检索相关条目，返回匹配标题与正文摘要。"""
    tokens = [t.lower() for t in query.split() if t.strip()]
    if not tokens:
        return "查询为空，请提供关键词。"

    scored: list[tuple[int, dict[str, str]]] = []
    for item in KNOWLEDGE_BASE:
        haystack = f"{item['title']} {item['tags']} {item['content']}".lower()
        score = sum(1 for t in tokens if t in haystack)
        if score > 0:
            scored.append((score, item))

    scored.sort(key=lambda x: x[0], reverse=True)
    hits = scored[: max(1, top_k)]
    if not hits:
        return (
            f"未找到与「{query}」相关的知识条目。"
            "你可以根据已有训练知识作答，并明确标注「非知识库来源」。"
        )

    blocks = [f"检索到 {len(hits)} 条相关知识：\n"]
    for score, item in hits:
        blocks.append(
            f"### [{item['id']}] {item['title']}（匹配分={score}）\n"
            f"{item['content']}\n"
        )
    return "\n".join(blocks)


@tool
def list_knowledge_topics() -> str:
    """列出知识库中全部主题，便于用户了解可问范围。"""
    lines = ["知识库主题列表："]
    for item in KNOWLEDGE_BASE:
        lines.append(f"- {item['id']}: {item['title']}  [{item['tags']}]")
    return "\n".join(lines)


@tool(args_schema=SaveQaNoteArgs)
def save_qa_note(question: str, answer: str, tags: str = "") -> str:
    """将一对高质量问答沉淀为笔记，便于后续复用。"""
    notes = _notes_dir()
    notes.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = notes / f"qa_{stamp}.md"
    body = (
        f"# Q&A Note\n\n"
        f"- time: {stamp}\n"
        f"- tags: {tags or 'general'}\n\n"
        f"## Question\n\n{question.strip()}\n\n"
        f"## Answer\n\n{answer.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    return f"问答笔记已保存：{path}"


QA_TOOLS = [
    search_knowledge,
    list_knowledge_topics,
    save_qa_note,
]
