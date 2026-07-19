"""请求 / 响应 schemas（对齐设计文档 §11）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ── Tool / Middleware briefs ─────────────────────────────────────────────


class ToolBrief(BaseModel):
    id: str
    name: str

    model_config = {"from_attributes": True}


class MiddlewareBrief(BaseModel):
    id: str
    name: str

    model_config = {"from_attributes": True}


# ── Agent ────────────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    methodology_id: str
    name: str
    system_prompt: str = ""
    model: str | None = None
    temperature: float | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    tool_ids: list[str] = Field(default_factory=list)
    middleware_ids: list[str] = Field(default_factory=list)
    id: str | None = None


class AgentUpdate(BaseModel):
    name: str | None = None
    system_prompt: str | None = None
    model: str | None = None
    temperature: float | None = None
    config: dict[str, Any] | None = None
    tool_ids: list[str] | None = None
    middleware_ids: list[str] | None = None


class AgentBindTools(BaseModel):
    tool_ids: list[str]
    replace: bool = True


class AgentBindMiddlewares(BaseModel):
    middleware_ids: list[str]
    replace: bool = True


class AgentOut(BaseModel):
    id: str
    methodology_id: str
    name: str
    system_prompt: str
    model: str | None
    temperature: float | None
    config: dict[str, Any]
    tools: list[ToolBrief] = Field(default_factory=list)
    middlewares: list[MiddlewareBrief] = Field(default_factory=list)

    model_config = {"from_attributes": True}


# ── Methodology ──────────────────────────────────────────────────────────


class MethodologyCreate(BaseModel):
    name: str
    description: str = ""
    id: str | None = None


class MethodologyUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    bump_version: bool = True


class MethodologyOut(BaseModel):
    id: str
    name: str
    description: str
    version: int
    status: str
    created_time: datetime
    updated_time: datetime

    model_config = {"from_attributes": True}


class MethodologyDetailOut(MethodologyOut):
    agents: list[AgentOut] = Field(default_factory=list)


# ── Tool / Middleware ────────────────────────────────────────────────────


class ToolCreate(BaseModel):
    name: str
    class_path: str
    description: str = ""
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    id: str | None = None


class ToolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    class_path: str | None = None
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    config: dict[str, Any] | None = None
    status: str | None = None


class ToolOut(BaseModel):
    id: str
    name: str
    description: str
    class_path: str
    input_schema: dict[str, Any] | None = None
    output_schema: dict[str, Any] | None = None
    config: dict[str, Any]
    status: str

    model_config = {"from_attributes": True}


class MiddlewareCreate(BaseModel):
    name: str
    class_path: str
    config: dict[str, Any] = Field(default_factory=dict)
    id: str | None = None


class MiddlewareUpdate(BaseModel):
    name: str | None = None
    class_path: str | None = None
    config: dict[str, Any] | None = None


class MiddlewareOut(BaseModel):
    id: str
    name: str
    class_path: str
    config: dict[str, Any]

    model_config = {"from_attributes": True}


# ── Conversation / Chat ──────────────────────────────────────────────────


class ConversationCreate(BaseModel):
    methodology_id: str
    user_id: str | None = None
    thread_id: str | None = None


class ConversationOut(BaseModel):
    id: str
    thread_id: str
    user_id: str | None
    methodology_id: str
    methodology_version: int
    created_time: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    thread_id: str
    message: str


class ChatResumeRequest(BaseModel):
    thread_id: str
    approve: bool = True


class ChatResponse(BaseModel):
    thread_id: str
    reply: str
    interrupted: bool = False
    interrupt: str | None = None
    methodology_id: str
    methodology_version: int


class ChatMessageOut(BaseModel):
    """会话历史中的单条消息（从 checkpointer state 提取）。"""

    role: str  # user | assistant | system | tool
    content: str
    name: str | None = None


class ConversationMessagesOut(BaseModel):
    thread_id: str
    methodology_id: str
    methodology_version: int
    messages: list[ChatMessageOut] = Field(default_factory=list)
    interrupted: bool = False
    interrupt: str | None = None
