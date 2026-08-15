"""
ORM 模型
========

表：model / methodology / agent / tool / skill / middleware 及关联表、
methodology_revision、conversation。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from deepagents_app.db.base import Base

JsonType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelDefinition(Base):
    """平台大模型目录：连接信息 + 超参数，供 Agent 勾选。"""

    __tablename__ = "model_definition"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_model_owner_name"),
        # 同一用户至多一条 is_default=True（部分唯一索引）
        Index(
            "uq_model_one_default_per_owner",
            "owner_user_id",
            unique=True,
            postgresql_where=text("is_default IS TRUE"),
            sqlite_where=text("is_default = 1"),
        ),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout: Mapped[float | None] = mapped_column(Float, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    is_default: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false(), nullable=False
    )
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list["AgentDefinition"]] = relationship(back_populates="llm_model")


class Methodology(Base):
    """方法论：勾选一组全局 Agent 组成的版本化配置包。"""

    __tablename__ = "methodology"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_methodology_owner_name"),
        Index("ix_methodology_owner_updated_id", "owner_user_id", "updated_time", "id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list["AgentDefinition"]] = relationship(
        secondary="methodology_agent",
        back_populates="methodologies",
        order_by="AgentDefinition.name",
    )


class AgentDefinition(Base):
    """全局 Agent：Supervisor 或 SubAgent，可被多个方法论勾选。"""

    __tablename__ = "agent_definition"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_agent_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    model_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("model_definition.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    llm_model: Mapped[ModelDefinition | None] = relationship(back_populates="agents")
    methodologies: Mapped[list[Methodology]] = relationship(
        secondary="methodology_agent",
        back_populates="agents",
    )
    tools: Mapped[list["ToolDefinition"]] = relationship(
        secondary="agent_tool",
        back_populates="agents",
    )
    middlewares: Mapped[list["MiddlewareDefinition"]] = relationship(
        secondary="agent_middleware",
        back_populates="agents",
    )
    skills: Mapped[list["SkillDefinition"]] = relationship(
        secondary="agent_skill",
        back_populates="agents",
        order_by="SkillDefinition.name",
    )


class ToolDefinition(Base):
    """
    工具元信息。

    - builtin：种子内置，class_path 指向 Python 对象
    - mcp：MCP Server 连接；运行时展开为工具列表
    - http：外部 API；config 含 url / input_schema，运行时展开为单个 StructuredTool
    """

    __tablename__ = "tool_definition"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_tool_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_type: Mapped[str] = mapped_column(String(32), default="builtin", nullable=False)
    class_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 调用前是否需 HITL；组装时并入 create_deep_agent(interrupt_on=...)
    requires_hitl: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_tool",
        back_populates="tools",
    )


class SkillDefinition(Base):
    """
    Skill 目录：完整 SKILL.md 存 content，运行时物化到 workspace。

    ``name`` 同时作为物化子目录名（须为安全 slug）。
    方法论快照不内嵌正文，只存 content_blob.hash 引用。
    """

    __tablename__ = "skill_definition"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_skill_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_skill",
        back_populates="skills",
    )


class ContentBlob(Base):
    """
    按内容哈希寻址的不可变正文库。

    快照通过 sha256 hex 引用；无任何 revision 引用时可 GC。
    """

    __tablename__ = "content_blob"

    hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    content: Mapped[str] = mapped_column(Text, default="", nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MiddlewareDefinition(Base):
    """内置 Middleware 元信息（仅种子写入，无对外新建/编辑 API）。"""

    __tablename__ = "middleware_definition"
    __table_args__ = (
        UniqueConstraint("owner_user_id", "name", name="uq_middleware_owner_name"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    class_path: Mapped[str] = mapped_column(String(512), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
    updated_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False
    )

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_middleware",
        back_populates="middlewares",
    )


class MethodologyAgent(Base):
    """方法论 ↔ 全局 Agent。"""

    __tablename__ = "methodology_agent"

    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("agent_definition.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class AgentTool(Base):
    """Agent ↔ Tool。"""

    __tablename__ = "agent_tool"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    tool_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("tool_definition.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class AgentMiddleware(Base):
    """Agent ↔ Middleware。"""

    __tablename__ = "agent_middleware"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    middleware_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("middleware_definition.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class AgentSkill(Base):
    """Agent ↔ Skill。"""

    __tablename__ = "agent_skill"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(128),
        ForeignKey("skill_definition.id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )


class MethodologyRevision(Base):
    """方法论版本快照。"""

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
    """会话：绑定方法论版本。"""

    __tablename__ = "conversation"
    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_conversation_user_thread"),
        Index("ix_conversation_user_created_id", "user_id", "created_time", "id"),
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id"), nullable=False, index=True
    )
    methodology_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
