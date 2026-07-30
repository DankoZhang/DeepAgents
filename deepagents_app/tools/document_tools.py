"""
文档撰写工具
============

供 ``document-writer`` 子 Agent 使用。

说明：
- deepagents 自带 ``write_file`` / ``edit_file`` / ``read_file`` 等文件系统工具，
  这里再提供一层**面向文档场景**的高层工具，降低模型心智负担。
- 工具内部仍落到 workspace，路径相对 ``workspace/documents/``。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from deepagents_app.config import get_settings


def _docs_root() -> Path:
    """文档默认落盘目录。"""
    return get_settings().workspace_dir / "documents"


def _safe_doc_path(filename: str) -> Path:
    """
    将用户给出的文件名解析到 documents 目录内，防止路径穿越。

    例如 ``../../etc/passwd`` 会被拒绝。
    """
    root = _docs_root().resolve()
    # 只取文件名部分，禁止子目录穿越
    name = Path(filename).name
    if not name:
        raise ValueError("文件名不能为空")
    target = (root / name).resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"非法路径：{filename}")
    return target


class CreateDocumentArgs(BaseModel):
    filename: str = Field(
        description="文件名，建议以 .md 结尾，例如 project-readme.md"
    )
    title: str = Field(description="文档标题（写入一级标题）")
    content: str = Field(description="文档正文（Markdown）")


class AppendDocumentSectionArgs(BaseModel):
    filename: str = Field(description="已有文档文件名")
    section_title: str = Field(description="章节标题")
    section_content: str = Field(description="章节正文")


class ReadDocumentArgs(BaseModel):
    filename: str = Field(description="文档文件名")


@tool(args_schema=CreateDocumentArgs)
def create_document(filename: str, title: str, content: str) -> str:
    """创建一份 Markdown 文档并写入工作区，返回路径与字数摘要。"""
    path = _safe_doc_path(filename if filename.endswith(".md") else f"{filename}.md")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = (
        f"# {title}\n\n"
        f"> 生成时间：{now}\n\n"
        f"{content.strip()}\n"
    )
    path.write_text(body, encoding="utf-8")
    return (
        f"文档已创建：{path}\n"
        f"- 标题：{title}\n"
        f"- 字数：{len(content)}\n"
        f"- 大小：{path.stat().st_size} bytes"
    )


@tool(args_schema=AppendDocumentSectionArgs)
def append_document_section(filename: str, section_title: str, section_content: str) -> str:
    """向已有 Markdown 文档追加一个二级章节。"""
    path = _safe_doc_path(filename if filename.endswith(".md") else f"{filename}.md")
    if not path.exists():
        return f"文档不存在：{path}。请先调用 create_document。"

    block = f"\n## {section_title}\n\n{section_content.strip()}\n"
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return f"已向 {path.name} 追加章节「{section_title}」。"


@tool
def list_documents() -> str:
    """列出工作区 documents 目录下的全部 Markdown 文档。"""
    root = _docs_root()
    files = sorted(root.glob("*.md"))
    if not files:
        return "documents 目录为空，尚无文档。"
    lines = ["现有文档："]
    for f in files:
        lines.append(f"- {f.name} ({f.stat().st_size} bytes)")
    return "\n".join(lines)


@tool(args_schema=ReadDocumentArgs)
def read_document(filename: str) -> str:
    """读取指定文档的完整内容。"""
    path = _safe_doc_path(filename if filename.endswith(".md") else f"{filename}.md")
    if not path.exists():
        return f"文档不存在：{path}"
    return path.read_text(encoding="utf-8")


# 子 Agent 注册用的工具列表
DOCUMENT_TOOLS = [
    create_document,
    append_document_section,
    list_documents,
    read_document,
]
