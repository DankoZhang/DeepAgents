"""
内置工具实现
============

按子 Agent 职责拆分工具模块；运行时由 DB ``class_path`` + Tool Registry 动态加载：

- ``document_tools``  → document-writer
- ``computer_tools``  → computer-operator
- ``qa_tools``        → qa-expert
"""
