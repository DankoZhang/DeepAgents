"""
自定义 Middleware 集合
======================

Middleware 是 deepagents / LangChain Agent 的横切扩展点。
本包演示三类常见用法：

1. ``LoggingMiddleware``  —— 观测：记录模型调用与工具调用
2. ``TimingMiddleware``   —— 性能：统计各阶段耗时
3. ``AuditMiddleware``    —— 合规：把敏感工具调用写入审计日志

合并方式：由方法论 Agent 勾选后，经 Middleware Registry 按 ``class_path``
实例化并传入 ``create_deep_agent(..., middleware=[...])``。

自定义 middleware 的 ``.name`` 若不与默认栈重名，会插入到核心中间件之后；
若 ``.name`` 与默认中间件相同，则会**原地替换**该默认实例。
"""

from deepagents_app.middleware.audit_middleware import AuditMiddleware
from deepagents_app.middleware.logging_middleware import LoggingMiddleware
from deepagents_app.middleware.timing_middleware import TimingMiddleware

__all__ = ["AuditMiddleware", "LoggingMiddleware", "TimingMiddleware"]
