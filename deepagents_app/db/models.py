"""
ORM 模型
========

表：
- model_definition（前端可配置的大模型目录）
- methodology / methodology_agent（方法论勾选全局 Agent）
- agent_definition（全局 Agent，经 model_id 绑定目录模型）
- tool_definition（builtin 内置 / mcp 前端新增）
- skill_definition（前端可配置的 Skills 目录；运行时物化到 workspace）
- middleware_definition（仅种子内置，无对外写接口）
- agent_tool / agent_middleware / agent_skill
- methodology_revision / conversation
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

JsonType = JSON().with_variant(JSONB(), "postgresql")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ModelDefinition(Base):
    """平台大模型目录：连接信息 + 超参数，供 Agent 勾选（方案 B）。"""

    __tablename__ = "model_definition"
    __table_args__ = (UniqueConstraint("name", name="uq_model_name"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    # openai | anthropic | openai_compatible
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="openai")
    model_name: Mapped[str] = mapped_column(String(256), nullable=False)
    api_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    base_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    top_p: Mapped[float | None] = mapped_column(Float, nullable=True)
    max_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 上下文窗口（元数据，供前端展示；不直接传给 SDK）
    context_length: Mapped[int | None] = mapped_column(Integer, nullable=True)
    timeout: Mapped[float | None] = mapped_column(Float, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
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

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
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

    # 多对多：方法论勾选的全局 Agent（删方法论只删关联，不删 Agent）
    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="methodology_agent",
        back_populates="methodologies",
        order_by="AgentDefinition.name",
    )


class AgentDefinition(Base):
    """全局 Agent：Supervisor 或 SubAgent，可被多个方法论勾选。"""

    __tablename__ = "agent_definition"
    __table_args__ = (UniqueConstraint("name", name="uq_agent_name"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 方案 B：绑定模型目录；组装时用目录超参数 build_chat_model
    model_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("model_definition.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 兼容旧字段 / 无目录时的兜底模型名与温度
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    temperature: Mapped[float | None] = mapped_column(Float, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    llm_model: Mapped[ModelDefinition | None] = relationship(back_populates="agents")
    methodologies: Mapped[list[Methodology]] = relationship(
        secondary="methodology_agent",
        back_populates="agents",
    )
    tools: Mapped[list[ToolDefinition]] = relationship(
        secondary="agent_tool",
        back_populates="agents",
    )
    middlewares: Mapped[list[MiddlewareDefinition]] = relationship(
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

    - builtin：种子内置，class_path 指向 Python 对象；前端只可勾选不可新建
    - mcp：前端新增的 MCP Server 连接；运行时展开为该 Server 下的工具列表
    """

    __tablename__ = "tool_definition"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # builtin | mcp
    tool_type: Mapped[str] = mapped_column(String(32), default="builtin", nullable=False)
    # builtin 必填；mcp 可为空
    class_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    input_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    output_schema: Mapped[dict[str, Any] | None] = mapped_column(JsonType, nullable=True)
    # mcp 时存连接：transport / command / args / url / env / headers / include_tools
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_tool",
        back_populates="tools",
    )


class SkillDefinition(Base):
    """
    Skill 目录：完整 SKILL.md 存 content，运行时物化到 workspace 供 SkillsMiddleware 读取。

    name 同时作为物化子目录名（须为安全 slug）；description 供列表展示与 frontmatter 对齐。
    """

    __tablename__ = "skill_definition"
    __table_args__ = (UniqueConstraint("name", name="uq_skill_name"),)

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # 全局唯一；亦作物化目录名，如 document-writing
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    # 完整 SKILL.md 文本（含 YAML frontmatter + 正文）
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


class MiddlewareDefinition(Base):
    """内置 Middleware 元信息（仅种子写入，无对外新建/编辑 API）。"""

    __tablename__ = "middleware_definition"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    class_path: Mapped[str] = mapped_column(String(512), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict, nullable=False)

    agents: Mapped[list[AgentDefinition]] = relationship(
        secondary="agent_middleware",
        back_populates="middlewares",
    )


class MethodologyAgent(Base):
    """方法论 ↔ 全局 Agent 多对多。"""

    __tablename__ = "methodology_agent"

    methodology_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("methodology.id", ondelete="CASCADE"), primary_key=True
    )
    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
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


class AgentSkill(Base):
    """Agent ↔ Skill 多对多。"""

    __tablename__ = "agent_skill"

    agent_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("agent_definition.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("skill_definition.id", ondelete="CASCADE"), primary_key=True
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
