"""
工具包总入口
============

按子 Agent 职责拆分工具模块，避免把所有工具塞进一个文件：

- ``document_tools``  → document-writer
- ``computer_tools``  → computer-operator
- ``qa_tools``        → qa-expert
"""

from deepagents_app.tools.computer_tools import COMPUTER_TOOLS
from deepagents_app.tools.document_tools import DOCUMENT_TOOLS
from deepagents_app.tools.qa_tools import QA_TOOLS

__all__ = ["COMPUTER_TOOLS", "DOCUMENT_TOOLS", "QA_TOOLS"]
