"""
子 Agent 规格总入口
==================

每个子 Agent 是一份符合 deepagents ``SubAgent`` 规范的字典：

必需字段：
- ``name``：唯一标识，主 Agent 通过 task(subagent_type=name) 调用
- ``description``：给主 Agent 看的「何时委派」说明（会出现在 task 工具描述里）
- ``system_prompt``：子 Agent 自己的系统提示

可选字段：
- ``tools``：专属工具列表
- ``middleware``：子 Agent 自己的中间件栈扩展
- ``model``：覆盖主 Agent 模型
- ``skills``：专属 skills 路径
"""

from deepagents_app.subagents.computer_operator import build_computer_operator_subagent
from deepagents_app.subagents.document_writer import build_document_writer_subagent
from deepagents_app.subagents.qa_agent import build_qa_subagent


def build_all_subagents() -> list[dict]:
    """构建本演示框架的全部同步子 Agent 规格。"""
    return [
        build_document_writer_subagent(),
        build_computer_operator_subagent(),
        build_qa_subagent(),
    ]


__all__ = [
    "build_all_subagents",
    "build_computer_operator_subagent",
    "build_document_writer_subagent",
    "build_qa_subagent",
]
