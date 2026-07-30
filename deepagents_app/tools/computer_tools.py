"""
计算机操作工具
==============

供 ``computer-operator`` 子 Agent 使用。

安全策略（演示级）：
1. 所有路径限制在 ``workspace_dir`` 内（路径穿越拦截）
2. shell 命令走白名单前缀，禁止 ``rm -rf /``、管道写系统目录等危险模式
3. 真实生产环境应改用 sandbox backend（Docker / Firecracker 等）

注意：deepagents 的 FilesystemBackend 也会提供 ``ls`` / ``read_file`` 等工具；
这里的工具更偏「显式、可审计」的业务封装，便于在 HITL 中单独拦截。
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from deepagents_app.config import get_settings

# 允许执行的命令前缀（演示白名单）
_ALLOWED_PREFIXES = (
    "ls",
    "pwd",
    "echo",
    "cat",
    "head",
    "tail",
    "wc",
    "find",
    "grep",
    "python",
    "python3",
    "pip",
    "pip3",
    "mkdir",
    "touch",
    "cp",
    "mv",
    "date",
    "uname",
    "which",
    "tree",
)

# 明显危险模式
_DANGEROUS_PATTERNS = (
    re.compile(r"\brm\b", re.I),
    re.compile(r"\bsudo\b", re.I),
    re.compile(r">\s*/"),  # 重定向到根路径
    re.compile(r"\bchmod\b", re.I),
    re.compile(r"\bchown\b", re.I),
    re.compile(r"\bcurl\b.*\|\s*(sh|bash)", re.I),
    re.compile(r"\bmkfs\b", re.I),
    re.compile(r":\(\)\s*\{"),  # fork bomb
)


def _workspace() -> Path:
    return get_settings().workspace_dir.resolve()


def _safe_path(relative_path: str) -> Path:
    """解析相对 workspace 的路径，拦截 ``..`` 穿越。"""
    root = _workspace()
    # 允许传入绝对路径，但必须仍落在 workspace 内
    candidate = Path(relative_path)
    target = (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    if not str(target).startswith(str(root)):
        raise ValueError(f"路径越界，仅允许访问 workspace：{relative_path}")
    return target


def _validate_command(command: str) -> str | None:
    """校验 shell 命令；不合法时返回错误信息，合法返回 None。"""
    stripped = command.strip()
    if not stripped:
        return "命令为空"

    for pat in _DANGEROUS_PATTERNS:
        if pat.search(stripped):
            return f"命令匹配危险模式，已拒绝：{pat.pattern}"

    try:
        parts = shlex.split(stripped)
    except ValueError as exc:
        return f"命令解析失败：{exc}"

    if not parts:
        return "命令为空"

    head = parts[0]
    # 允许 ``python -c`` 等，但首 token 必须在白名单
    if not any(head == p or head.endswith(f"/{p}") for p in _ALLOWED_PREFIXES):
        return (
            f"命令「{head}」不在白名单。允许："
            + ", ".join(_ALLOWED_PREFIXES)
        )
    return None


class ListWorkspaceArgs(BaseModel):
    relative_dir: str = Field(
        default=".",
        description="相对 workspace 的目录，默认 .（根目录）",
    )


class ReadWorkspaceFileArgs(BaseModel):
    relative_path: str = Field(description="相对 workspace 的文件路径")
    max_chars: int = Field(
        default=8000,
        ge=1,
        le=100_000,
        description="最多返回的字符数，防止撑爆上下文",
    )


class WriteWorkspaceFileArgs(BaseModel):
    relative_path: str = Field(description="相对 workspace 的目标路径")
    content: str = Field(description="文件内容")
    overwrite: bool = Field(
        default=False,
        description="若文件已存在，是否允许覆盖；默认 False",
    )


class RunShellCommandArgs(BaseModel):
    command: str = Field(description="要执行的命令字符串（须在白名单内）")
    timeout_seconds: int = Field(
        default=30,
        ge=1,
        le=300,
        description="超时秒数，默认 30",
    )


@tool(args_schema=ListWorkspaceArgs)
def list_workspace(relative_dir: str = ".") -> str:
    """列出 workspace 下某目录的文件与子目录。"""
    path = _safe_path(relative_dir)
    if not path.exists():
        return f"目录不存在：{path}"
    if not path.is_dir():
        return f"不是目录：{path}"

    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    if not entries:
        return f"{path} 为空目录。"

    lines = [f"{path} 内容："]
    for entry in entries:
        kind = "DIR " if entry.is_dir() else "FILE"
        size = "-" if entry.is_dir() else f"{entry.stat().st_size}B"
        lines.append(f"  [{kind}] {entry.name:40s} {size}")
    return "\n".join(lines)


@tool(args_schema=ReadWorkspaceFileArgs)
def read_workspace_file(relative_path: str, max_chars: int = 8000) -> str:
    """读取 workspace 内文本文件内容（带长度截断）。"""
    path = _safe_path(relative_path)
    if not path.exists():
        return f"文件不存在：{path}"
    if not path.is_file():
        return f"不是文件：{path}"

    text = path.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n\n...[已截断，原文共 {len(text)} 字符]"
    return text


@tool(args_schema=WriteWorkspaceFileArgs)
def write_workspace_file(relative_path: str, content: str, overwrite: bool = False) -> str:
    """向 workspace 写入文本文件。"""
    path = _safe_path(relative_path)
    if path.exists() and not overwrite:
        return f"文件已存在且 overwrite=False，拒绝写入：{path}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字符）"


@tool(args_schema=RunShellCommandArgs)
def run_shell_command(command: str, timeout_seconds: int = 30) -> str:
    """在 workspace 目录下执行白名单内的 shell 命令，返回 stdout/stderr 摘要。"""
    err = _validate_command(command)
    if err:
        return f"[拒绝执行] {err}"

    cwd = _workspace()
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"[超时] 命令超过 {timeout_seconds}s：{command}"
    except OSError as exc:
        return f"[系统错误] {exc}"

    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    parts = [
        f"exit_code={completed.returncode}",
        f"cwd={cwd}",
        f"command={command}",
    ]
    if stdout:
        parts.append(f"--- stdout ---\n{stdout}")
    if stderr:
        parts.append(f"--- stderr ---\n{stderr}")
    if not stdout and not stderr:
        parts.append("(无输出)")
    return "\n".join(parts)


COMPUTER_TOOLS = [
    list_workspace,
    read_workspace_file,
    write_workspace_file,
    run_shell_command,
]
