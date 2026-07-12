"""
DeepAgents 演示框架
====================

基于 LangChain ``deepagents`` 构建的完整多智能体示例：

- **主 Agent（Supervisor）**：负责任务理解、拆解与调度
- **document-writer**：文档撰写子 Agent
- **computer-operator**：计算机操作子 Agent
- **qa-expert**：智能问答子 Agent

并演示自定义 Middleware、Filesystem Backend、Memory、Skills、
Human-in-the-loop、Checkpointer 等 deepagents 核心能力。

快速开始::

    from deepagents_app.factory import build_deep_agent

    agent = build_deep_agent()
    result = agent.invoke(
        {"messages": [{"role": "user", "content": "写一份项目 README"}]},
        config={"configurable": {"thread_id": "demo-1"}},
    )
"""

from deepagents_app.factory import build_deep_agent

__all__ = ["build_deep_agent"]
__version__ = "0.1.0"
