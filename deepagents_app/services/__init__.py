"""
业务服务层包入口
================

对外再导出常用符号；路由一般直接 ``from deepagents_app.services import xxx as svc``。
"""

# 方法论驱动组装 Compiled Agent
from deepagents_app.services.agent_factory import (
    build_agent_from_methodology,
    invalidate_agent_cache,
)
# 一轮对话（别名避免与路由函数名冲突）
from deepagents_app.services.chat import chat as run_chat
# 创建会话登记
from deepagents_app.services.conversation import create_conversation

# 明确公开 API，避免 from services import * 拉进过多符号
__all__ = [
    "build_agent_from_methodology",
    "invalidate_agent_cache",
    "create_conversation",
    "run_chat",
]
