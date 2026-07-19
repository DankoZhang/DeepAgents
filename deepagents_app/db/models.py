"""
ORM 模型（对应设计文档 §5 / §6）
================================

表：
- methodology
- agent_definition
- tool_definition
- middleware_definition
- agent_tool / agent_middleware（关系表）
- conversation
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from deepagents_app.db.base import Base

# SQLite 测试兼容：PostgreSQL 用 JSONB，其他方言用 JSON
JsonType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Methodology(Base):
    """方法论：一组 Supervisor + SubAgent 的版本化配置包。"""

    __tablename__ = "methodology"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    # draft | published | archived
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list[AgentDefinition]] = relationship(
        back_populates="methodology",
        cascade="all, delete-orphan",
    )


class AgentDefinition(Base):
    """Agent 定义：Supervisor 或 SubAgent。"""

    __tablename__ = "agent_definition"
    __table_args__ = (
        UniqueConstraint("methodology_id", "name", name="uq_agent_methodology_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    # role / description / skills / interrupt_on / enabled 等扩展字段
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    methodology: Mapped[Methodology] = relationship(back_populates="agents")
    tools: Mapped[list[ToolDefinition]] = relationship(
        secondary="agent_tool",
        back_populates="agents",
    )
    middlewares: Mapped[list[MiddlewareDefinition]] = relationship(
        secondary="agent_middleware",
        back_populates="agents",
    )


class ToolDefinition(Base):
    """工具元信息（不存 Python 代码，仅 class_path）。"""

    __tablename__ = "tool_definition"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 例：deepagents_app.tools.document_tools:create_document
    class_path: Mapped[str] = mapped_column(String(512), nullable=False)
    # 设计文档 §4.3：可选 JSON Schema，供前端/校验展示（运行时仍以 Python 工具签名为准）
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    # active | disabled
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_tool",
        back_populates="tools",
    )


class MiddlewareDefinition(Base):
    """Middleware 元信息。"""

    __tablename__ = "middleware_definition"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    # 例：deepagents_app.middleware.logging_middleware:LoggingMiddleware
    class_path: Mapped[str] = mapped_column(String(512), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_middleware",
        back_populates="middlewares",
    )


class AgentTool(Base):
    """Agent ↔ Tool 多对多。"""

    __tablename__ = "agent_tool"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("tool_definition.id", ondelete="CASCADE"), primary_key=True
    )


class AgentMiddleware(Base):
    """Agent ↔ Middleware 多对多。"""

    __tablename__ = "agent_middleware"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    middleware_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("middleware_definition.id", ondelete="CASCADE"),
        primary_key=True,
    )


class MethodologyRevision(Base):
    """方法论版本快照：旧会话按创建时版本重建 Agent。"""

    __tablename__ = "methodology_revision"
    __table_args__ = (
        UniqueConstraint("methodology_id", "version", name="uq_methodology_revision"),
    )

    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class Conversation(Base):
    """会话：绑定方法论版本，thread_id 隔离多轮状态。"""

    __tablename__ = "conversation"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    user_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id"), nullable=False
    )
    methodology_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
