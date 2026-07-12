"""
计算机操作子 Agent
==================

专责：在 workspace 沙箱内进行文件浏览、读写与白名单 shell 执行。
"""

from __future__ import annotations

from deepagents_app.middleware.audit_middleware import AuditMiddleware
from deepagents_app.middleware.logging_middleware import LoggingMiddleware
from deepagents_app.middleware.timing_middleware import TimingMiddleware
from deepagents_app.tools.computer_tools import COMPUTER_TOOLS

COMPUTER_OPERATOR_PROMPT = """\
你是谨慎的**计算机操作 Agent**。

## 目标
在受限 workspace 内完成文件与命令行操作，并把可复核的结果回传给主 Agent。

## 可用工具
- `list_workspace`：列目录
- `read_workspace_file`：读文件
- `write_workspace_file`：写文件（注意 overwrite 默认 False）
- `run_shell_command`：执行白名单 shell 命令

## 工作流程
1. 先 `list_workspace` 确认当前目录结构，再动手
2. 修改前先 `read_workspace_file`（若目标已存在）
3. 执行命令后检查 exit_code 与 stderr
4. 汇报：做了什么、结果是什么、是否有告警

## 安全红线
- 不得尝试逃逸 workspace
- 不得绕过命令白名单
- 破坏性操作若任务未明确授权，应拒绝并说明原因
- 命令失败时如实报告，不要编造成功结果

## 边界
- 不做长文档创作（应改派 document-writer）
- 不做纯知识问答（应改派 qa-expert）
"""


def build_computer_operator_subagent() -> dict:
    """返回 deepagents SubAgent 规格字典。"""
    return {
        "name": "computer-operator",
        "description": (
            "计算机操作专家。适用于：浏览 workspace、读写文件、"
            "执行白名单内的 shell 命令（ls/python/grep 等），返回可复核的执行结果。"
        ),
        "system_prompt": COMPUTER_OPERATOR_PROMPT,
        "tools": list(COMPUTER_TOOLS),
        # 计算机操作风险更高：额外挂审计中间件
        "middleware": [
            LoggingMiddleware(),
            TimingMiddleware(),
            AuditMiddleware(),
        ],
    }
