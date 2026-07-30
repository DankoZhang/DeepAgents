"""
ORM 模型
========

表：
- methodology / methodology_agent（方法论勾选全局 Agent）
- agent_definition（全局 Agent）
- tool_definition（builtin 内置 / mcp 前端新增）
- middleware_definition（仅种子内置，无对外写接口）
- agent_tool / agent_middleware
- methodology_revision / conversation
"""

# 启用延迟注解求值，便于前向引用类型（如 Methodology 引用尚未定义的 AgentDefinition）
from __future__ import annotations

# 导入 datetime 与 timezone，用于记录创建/更新时间（UTC）
from datetime import datetime, timezone
# 导入 Any，用于 JSON 字段的类型标注
from typing import Any

# 从 SQLAlchemy 导入常用列类型与约束
from sqlalchemy import (
    DateTime,  # 日期时间列类型
    Float,  # 浮点列类型（如 temperature）
    ForeignKey,  # 外键约束
    Integer,  # 整型列类型（如 version）
    String,  # 定长/变长字符串列类型
    Text,  # 长文本列类型
    UniqueConstraint,  # 唯一约束（表级）
)
# 导入 PostgreSQL 专用的 JSONB 类型（比 JSON 查询性能更好）
from sqlalchemy.dialects.postgresql import JSONB
# 导入 ORM 映射辅助：Mapped 类型注解、mapped_column 列定义、relationship 关系
from sqlalchemy.orm import Mapped, mapped_column, relationship
# 导入通用 JSON 类型，作为非 PostgreSQL 数据库的回退
from sqlalchemy.types import JSON

# 导入项目 declarative 基类，所有模型都继承自它
from deepagents_app.db.base import Base

# 定义跨数据库 JSON 列：默认用 JSON，在 PostgreSQL 上自动切换为 JSONB
JsonType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    # 返回当前 UTC 时间，供 created_time / updated_time 的 default 与 onupdate 使用
    return datetime.now(timezone.utc)


class Methodology(Base):
    """方法论：勾选一组全局 Agent 组成的版本化配置包。"""

    # 对应数据库表名
    __tablename__ = "methodology"

    # 主键：方法论唯一 ID
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 方法论显示名称，非空
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # 方法论描述，默认空字符串
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 当前版本号，默认从 1 开始
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # 状态（如 draft / published），默认 draft
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    # 创建时间（带时区），插入时自动填入 UTC 当前时间
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    # 更新时间（带时区），插入与更新时都会刷新为 UTC 当前时间
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    # 多对多：方法论勾选的全局 Agent（删方法论只删关联，不删 Agent）
    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="methodology_agent",  # 通过中间表 methodology_agent 关联
        back_populates="methodologies",  # 与 AgentDefinition.methodologies 双向同步
        order_by="AgentDefinition.name",  # 按 Agent 名称排序返回
    )


class AgentDefinition(Base):
    """全局 Agent：Supervisor 或 SubAgent，可被多个方法论勾选。"""

    # 对应数据库表名
    __tablename__ = "agent_definition"
    # 表级约束：Agent 名称全局唯一
    __table_args__ = (UniqueConstraint("name", name="uq_agent_name"),)

    # 主键：Agent 唯一 ID
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # Agent 名称（唯一约束见上），非空
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    # 系统提示词，默认空字符串
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 使用的模型名，可为空（走默认模型）
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 采样温度，可为空（走默认温度）
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    # 额外配置 JSON（扩展字段），默认空字典
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    # 多对多：该 Agent 被哪些方法论勾选
    methodologies: Mapped[list[Methodology]] = relationship(
        secondary="methodology_agent",  # 通过中间表 methodology_agent 关联
        back_populates="agents",  # 与 Methodology.agents 双向同步
    )
    # 多对多：该 Agent 绑定的工具列表
    tools: Mapped[list[ToolDefinition]] = relationship(
        secondary="agent_tool",  # 通过中间表 agent_tool 关联
        back_populates="agents",  # 与 ToolDefinition.agents 双向同步
    )
    # 多对多：该 Agent 绑定的中间件列表
    middlewares: Mapped[list[MiddlewareDefinition]] = relationship(
        secondary="agent_middleware",  # 通过中间表 agent_middleware 关联
        back_populates="agents",  # 与 MiddlewareDefinition.agents 双向同步
    )


class ToolDefinition(Base):
    """
    工具元信息。

    - builtin：种子内置，class_path 指向 Python 对象；前端只可勾选不可新建
    - mcp：前端新增的 MCP Server 连接；运行时展开为该 Server 下的工具列表
    """

    # 对应数据库表名
    __tablename__ = "tool_definition"

    # 主键：工具唯一 ID
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 工具名称，全局唯一且非空
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # 工具描述，默认空字符串
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 工具类型：builtin | mcp，默认 builtin
    tool_type: Mapped[str] = mapped_column(String(32), default="builtin", nullable=False)
    # Python 类/对象导入路径；builtin 必填，mcp 可为空
    class_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 输入参数 JSON Schema，可为空
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # 输出结果 JSON Schema，可为空
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # mcp 时存连接：transport / command / args / url / env / headers / include_tools
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    # 工具状态（如 active / disabled），默认 active
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    # 多对多：引用该工具的 Agent 列表
    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_tool",  # 通过中间表 agent_tool 关联
        back_populates="tools",  # 与 AgentDefinition.tools 双向同步
    )


class MiddlewareDefinition(Base):
    """内置 Middleware 元信息（仅种子写入，无对外新建/编辑 API）。"""

    # 对应数据库表名
    __tablename__ = "middleware_definition"

    # 主键：中间件唯一 ID
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 中间件名称，全局唯一且非空
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # Python 类导入路径，非空（运行时据此实例化）
    class_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # 中间件初始化配置 JSON，默认空字典
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    # 多对多：引用该中间件的 Agent 列表
    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_middleware",  # 通过中间表 agent_middleware 关联
        back_populates="middlewares",  # 与 AgentDefinition.middlewares 双向同步
    )


class MethodologyAgent(Base):
    """方法论 ↔ 全局 Agent 多对多。"""

    # 对应数据库表名（关联表）
    __tablename__ = "methodology_agent"

    # 复合主键之一：方法论 ID；删除方法论时级联删除本行
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), primary_key=True
    )
    # 复合主键之二：Agent ID；删除 Agent 时级联删除本行
    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )


class AgentTool(Base):
    """Agent ↔ Tool 多对多。"""

    # 对应数据库表名（关联表）
    __tablename__ = "agent_tool"

    # 复合主键之一：Agent ID；删除 Agent 时级联删除本行
    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    # 复合主键之二：工具 ID；删除工具时级联删除本行
    tool_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("tool_definition.id", ondelete="CASCADE"), primary_key=True
    )


class AgentMiddleware(Base):
    """Agent ↔ Middleware 多对多。"""

    # 对应数据库表名（关联表）
    __tablename__ = "agent_middleware"

    # 复合主键之一：Agent ID；删除 Agent 时级联删除本行
    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    # 复合主键之二：中间件 ID；删除中间件时级联删除本行
    middleware_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("middleware_definition.id", ondelete="CASCADE"),
        primary_key=True,
    )


class MethodologyRevision(Base):
    """方法论版本快照。"""

    # 对应数据库表名
    __tablename__ = "methodology_revision"
    # 同一方法论下 version 唯一，避免重复快照
    __table_args__ = (
        UniqueConstraint("methodology_id", "version", name="uq_methodology_revision"),
    )

    # 主键：修订记录唯一 ID
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    # 所属方法论 ID；删除方法论时级联删除本修订
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), nullable=False
    )
    # 该快照对应的版本号
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 版本内容快照（JSON），默认空字典
    snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    # 快照创建时间（带时区），插入时自动填入 UTC 当前时间
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Conversation(Base):
    """会话：绑定方法论版本。"""

    # 对应数据库表名
    __tablename__ = "conversation"

    # 主键：会话唯一 ID
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # LangGraph / 聊天线程 ID，唯一且建索引，便于按线程查找
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    # 发起会话的用户 ID，可为空（匿名或系统会话）
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # 绑定的方法论 ID（外键，非空）
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id"), nullable=False
    )
    # 会话创建时锁定的方法论版本号，保证对话期间配置一致
    methodology_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # 会话创建时间（带时区），插入时自动填入 UTC 当前时间
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
