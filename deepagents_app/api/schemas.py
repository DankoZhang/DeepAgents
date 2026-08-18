#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   schemas.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   schemas.py

请求 / 响应 schemas（Pydantic）
==============================

职责：校验 API 入参、约束字段类型，并把 ORM 对象序列化为响应 JSON。
``model_config = {"from_attributes": True}`` 表示可从 SQLAlchemy 模型属性直接构造。
"""

# 推迟注解求值，允许前向引用
from __future__ import annotations

# datetime：会话/方法论时间戳字段类型
from datetime import datetime
# Any：JSON 扩展字段；Literal：限制枚举取值
from typing import Any, Literal

# BaseModel：schema 基类；Field：默认值/工厂；model_validator：跨字段校验
from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from deepagents_app.constants import ModelProvider


# ── Tool / Middleware briefs（嵌套在 Agent 响应里的精简摘要）──────────────


class ToolBrief(BaseModel):
    """Agent 已绑定工具的简要信息（详情页列表用，避免整份 ToolOut）。"""

    id: str  # 工具主键
    name: str  # 工具名
    tool_type: str = "builtin"  # builtin | mcp | http，前端可用不同 Tag 展示

    # 允许 Agent.tools 关系里的 ToolDefinition ORM 对象直接转本模型
    model_config = {"from_attributes": True}


class MiddlewareBrief(BaseModel):
    """Agent 已绑定中间件的简要信息。"""

    id: str  # 中间件主键
    name: str  # 显示名，如 LoggingMiddleware

    model_config = {"from_attributes": True}


class SkillBrief(BaseModel):
    """Agent 已绑定 Skill 的简要信息。"""

    id: str
    name: str
    description: str = ""
    status: str = "active"

    model_config = {"from_attributes": True}


# ── Model（大模型目录）──────────────────────────────────────────────────


class ModelCreate(BaseModel):
    """POST /api/model：前端配置一条大模型。"""

    name: str  # 显示名，全局唯一
    provider: ModelProvider = "openai"
    model_name: str  # 提供商侧模型 ID，如 gpt-4o / deepseek-chat
    api_key: str | None = None
    base_url: str | None = None  # openai_compatible 常用
    temperature: float | None = 0.2
    top_p: float | None = None
    max_tokens: int | None = None  # 单次生成上限
    timeout: float | None = None
    config: dict[str, Any] = Field(default_factory=dict)  # 额外 SDK 参数
    status: str = "active"
    is_default: bool = False  # 新建默认关闭；开启则取消同用户其他默认
    id: str | None = None


class ModelUpdate(BaseModel):
    """PATCH /api/model/{id}。"""

    name: str | None = None
    provider: ModelProvider | None = None
    model_name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None
    config: dict[str, Any] | None = None
    status: str | None = None
    is_default: bool | None = None  # True 时同用户其他模型自动取消默认


class ModelOut(BaseModel):
    """模型目录响应（不回传明文 api_key）。"""

    id: str
    name: str
    provider: str
    model_name: str
    base_url: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    timeout: float | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    status: str
    is_default: bool = False
    created_time: datetime
    updated_time: datetime
    # 由 ORM api_key 派生，不在响应中暴露密钥本身
    api_key: str | None = Field(default=None, exclude=True)

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_api_key(self) -> bool:
        from deepagents_app.crypto import secret_is_present

        return secret_is_present(self.api_key)


class ModelBrief(BaseModel):
    """嵌在 Agent 响应里的模型摘要。"""

    id: str
    name: str
    provider: str
    model_name: str
    temperature: float | None = None
    top_p: float | None = None
    max_tokens: int | None = None
    status: str = "active"
    is_default: bool = False

    model_config = {"from_attributes": True}


class ModelTestRequest(BaseModel):
    """POST /api/model/test：按 id 或内联配置试连。"""

    model_id: str | None = None
    provider: ModelProvider | None = None
    model_name: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    temperature: float | None = 0.0
    top_p: float | None = None
    max_tokens: int | None = 16
    timeout: float | None = 30.0
    config: dict[str, Any] | None = None


class ModelTestResult(BaseModel):
    ok: bool
    message: str
    reply_preview: str | None = None


# ── Agent（全局）─────────────────────────────────────────────────────────


class AgentCreate(BaseModel):
    """POST /api/agent 请求体：创建全局 Agent。"""

    name: str  # 全局唯一名称
    system_prompt: str = ""  # 系统提示词
    model_id: str | None = None  # 绑定模型目录；缺省用当前用户 is_default 模型
    # 扩展字段：role(supervisor|subagent) / description / enabled /
    # subagent_ids / methodology_id 等。enabled 仅能经启用/停用接口改。
    config: dict[str, Any] = Field(default_factory=dict)
    tool_ids: list[str] = Field(default_factory=list)  # 创建时一并绑定的工具 id
    middleware_ids: list[str] = Field(default_factory=list)  # 创建时一并绑定的中间件 id
    skill_ids: list[str] = Field(default_factory=list)  # 创建时一并绑定的 Skill id
    id: str | None = None  # 可选客户端指定主键；否则服务端生成


class AgentUpdate(BaseModel):
    """PATCH /api/agent/{id}：字段均为可选，None 表示不修改。"""

    name: str | None = None
    system_prompt: str | None = None
    model_id: str | None = None  # 传空串则回落当前用户 is_default 模型
    config: dict[str, Any] | None = None  # 与现有 config 做 merge，非整表替换语义由 service 定
    tool_ids: list[str] | None = None  # 传入则整表替换绑定
    middleware_ids: list[str] | None = None
    skill_ids: list[str] | None = None


class AgentBindTools(BaseModel):
    """POST /api/agent/{id}/tools：单独改工具绑定。"""

    tool_ids: list[str]  # 目标工具 id 列表
    replace: bool = True  # True 先清空再绑；False 增量追加


class AgentBindMiddlewares(BaseModel):
    """POST /api/agent/{id}/middlewares：单独改中间件绑定。"""

    middleware_ids: list[str]
    replace: bool = True


class AgentBindSkills(BaseModel):
    """POST /api/agent/{id}/skills：单独改 Skill 绑定。"""

    skill_ids: list[str]
    replace: bool = True


class AgentOut(BaseModel):
    """
    Agent 详情/列表响应。

    ``enabled`` / ``methodology_id`` 从 ``config`` 抽出，方便前端不必再翻 JSON。
    """

    id: str
    name: str
    system_prompt: str
    model_id: str | None = None
    config: dict[str, Any]
    llm_model: ModelBrief | None = None
    tools: list[ToolBrief] = Field(default_factory=list)  # 已绑定工具摘要
    middlewares: list[MiddlewareBrief] = Field(default_factory=list)  # 已绑定中间件摘要
    skills: list[SkillBrief] = Field(default_factory=list)  # 已绑定 Skill 摘要

    model_config = {"from_attributes": True}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def enabled(self) -> bool:
        """是否已启用（启用后不可编辑；主 Agent 启用即发布方法论）。"""
        return bool((self.config or {}).get("enabled"))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def methodology_id(self) -> str | None:
        """主 Agent 启用后关联的方法论 id；子 Agent 通常为空。"""
        mid = (self.config or {}).get("methodology_id")
        return str(mid) if mid else None


# ── Methodology ──────────────────────────────────────────────────────────


class MethodologyCreate(BaseModel):
    """POST /api/methodology：创建草稿方法论。"""

    name: str
    description: str = ""
    id: str | None = None  # 可选指定 id；否则由名称 slug + 随机后缀生成
    # 创建时可直接勾选一批全局 Agent（也可稍后 POST .../agents）
    agent_ids: list[str] = Field(default_factory=list)


class MethodologyUpdate(BaseModel):
    """PATCH /api/methodology/{id}：仅改元信息（不升版）。"""

    name: str | None = None
    description: str | None = None


class MethodologyBindAgents(BaseModel):
    """POST /api/methodology/{id}/agents：勾选全局 Agent。"""

    agent_ids: list[str]  # 要纳入该方法论的 Agent id
    replace: bool = True  # True 替换整份勾选列表


class MethodologyOut(BaseModel):
    """方法论列表项 / 发布后简要响应。"""

    id: str
    name: str
    description: str
    version: int  # 配置版本号；会话会锁定创建时的 version
    status: str  # draft | published | archived
    created_time: datetime
    updated_time: datetime

    model_config = {"from_attributes": True}


class MethodologyDetailOut(MethodologyOut):
    """方法论详情：在列表字段基础上附带已勾选的 Agent 完整信息。"""

    agents: list[AgentOut] = Field(default_factory=list)


# ── Tool / Middleware ────────────────────────────────────────────────────


class McpServerConfig(BaseModel):
    """
    MCP Server 连接配置（前端新增工具时填写，存入 ToolDefinition.config）。

    运行时由 langchain-mcp-adapters 按这些字段连接 Server，并拉取其工具列表。
    """

    # 传输协议：决定后面用 command 还是 url
    # - stdio：本机拉起子进程（默认禁用，需 MCP_STDIO_ENABLED + 命令白名单）
    # - sse：HTTP Server-Sent Events（旧式远程 MCP）
    # - streamable_http：HTTP 流式传输（较新的远程 MCP）
    transport: Literal["stdio", "sse", "streamable_http"] = "streamable_http"
    # stdio 必填：可执行文件，如 npx / uvx / python
    command: str | None = None
    # stdio 可选：传给 command 的参数列表，如 ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]
    args: list[str] = Field(default_factory=list)
    # sse / streamable_http 必填：MCP HTTP 端点，如 https://mcp.example.com/mcp
    url: str | None = None
    # 传给 MCP 进程/连接的环境变量（如 API Key），键值均为字符串
    env: dict[str, str] = Field(default_factory=dict)
    # 仅 HTTP 类传输：额外请求头（如 Authorization）
    headers: dict[str, str] = Field(default_factory=dict)
    # 工具名白名单：只把 Server 上这些工具挂给 Agent；None = 暴露该 Server 全部工具
    include_tools: list[str] | None = None

    @model_validator(mode="after")  # 字段填完后做跨字段校验
    def _check_transport(self) -> McpServerConfig:
        # 形状校验；安全策略（stdio 开关 / SSRF）在服务层 validate_mcp_config
        if self.transport == "stdio":
            if not self.command:
                raise ValueError("stdio 传输需要提供 command")
        elif self.transport in {"sse", "streamable_http"}:
            if not self.url:
                raise ValueError(f"{self.transport} 传输需要提供 url")
        return self


class HttpToolConfig(BaseModel):
    """
    HTTP 工具配置（前端新增工具时填写，存入 ToolDefinition.config）。

    运行时按 input_schema 生成 LangChain StructuredTool，再按 param_in
    把模型填的参数映射到 path / query / json body / header。
    """

    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "GET"
    url: str  # 可含 {city} 等 path 占位符；主机名不能是占位符
    # 给 LLM 的 JSON Schema，必须是 type=object
    input_schema: dict[str, Any]
    # 参数落点；未声明的字段按 method 默认：GET/DELETE→query，其余→body
    param_in: dict[str, Literal["path", "query", "body", "header"]] = Field(
        default_factory=dict
    )
    headers: dict[str, str] = Field(default_factory=dict)  # 静态头（鉴权等）
    timeout: float = 15  # 秒；服务层限制 1–60

    @field_validator("method", mode="before")
    @classmethod
    def _upper_method(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @model_validator(mode="after")
    def _check_schema(self) -> HttpToolConfig:
        if not str(self.url or "").strip():
            raise ValueError("http 工具需要提供 url")
        schema = self.input_schema
        if schema.get("type") != "object":
            raise ValueError("input_schema.type 必须是 object")
        props = schema.get("properties")
        if not isinstance(props, dict):
            raise ValueError("input_schema.properties 必须是对象")
        unknown = set(self.param_in) - set(props)
        if unknown:
            raise ValueError(f"param_in 含未知参数：{', '.join(sorted(unknown))}")
        return self


class ToolCreate(BaseModel):
    """POST /api/tool：创建 MCP 或 HTTP 工具（内置工具由种子写入）。"""

    name: str  # 全局唯一名；http 时亦为运行时 LangChain tool.name
    description: str = ""
    tool_type: Literal["mcp", "http"] | None = None  # 缺省时按 mcp/http 块推断
    mcp: McpServerConfig | None = None  # tool_type=mcp 时必填
    http: HttpToolConfig | None = None  # tool_type=http 时必填
    requires_hitl: bool | None = None  # mcp 默认 True，http 默认 False
    status: str = "active"  # active | disabled
    id: str | None = None  # 可选指定主键

    @model_validator(mode="after")
    def _check_spec(self) -> ToolCreate:
        if self.mcp is not None and self.http is not None:
            raise ValueError("不能同时提供 mcp 和 http")
        tool_type = self.tool_type
        if tool_type is None:
            if self.http is not None:
                tool_type = "http"
            elif self.mcp is not None:
                tool_type = "mcp"
            else:
                raise ValueError("需要提供 mcp 或 http 配置")
            self.tool_type = tool_type
        if tool_type == "mcp":
            if self.mcp is None:
                raise ValueError("mcp 工具必须提供 mcp")
        elif tool_type == "http":
            if self.http is None:
                raise ValueError("http 工具必须提供 http")
        return self


class ToolUpdate(BaseModel):
    """PATCH /api/tool/{id}：部分更新；builtin 仅可改 status / requires_hitl。"""

    name: str | None = None
    description: str | None = None
    mcp: McpServerConfig | None = None  # 仅 mcp 类型可更新连接
    http: HttpToolConfig | None = None  # 仅 http 类型可更新接口配置
    requires_hitl: bool | None = None  # builtin / mcp / http 均可改
    status: str | None = None

    @model_validator(mode="after")
    def _check_spec(self) -> ToolUpdate:
        if self.mcp is not None and self.http is not None:
            raise ValueError("不能同时提供 mcp 和 http")
        return self


class ToolOut(BaseModel):
    """工具列表/详情响应。"""

    id: str
    name: str
    description: str
    tool_type: str  # builtin | mcp | http
    class_path: str | None = None  # builtin 有值；mcp / http 一般为 None
    requires_hitl: bool = False
    config: dict[str, Any]  # builtin 扩展配置，或 mcp / http 的执行 dict
    status: str

    model_config = {"from_attributes": True}


class ToolTestRequest(BaseModel):
    """POST /api/tool/test：按 id 或内联 mcp/http 配置试连。"""

    tool_id: str | None = None
    tool_type: Literal["mcp", "http"] | None = None
    mcp: McpServerConfig | None = None
    http: HttpToolConfig | None = None

    @model_validator(mode="after")
    def _check_spec(self) -> ToolTestRequest:
        if self.tool_id:
            return self
        if self.mcp is not None and self.http is not None:
            raise ValueError("不能同时提供 mcp 和 http")
        tool_type = self.tool_type
        if tool_type is None:
            if self.http is not None:
                tool_type = "http"
            elif self.mcp is not None:
                tool_type = "mcp"
            else:
                raise ValueError("未传 tool_id 时需要提供 mcp 或 http 配置")
            self.tool_type = tool_type
        if tool_type == "mcp" and self.mcp is None:
            raise ValueError("mcp 试连必须提供 mcp")
        if tool_type == "http" and self.http is None:
            raise ValueError("http 试连必须提供 http")
        return self


class ToolTestResult(BaseModel):
    ok: bool
    message: str
    detail: str | None = None


class MiddlewareOut(BaseModel):
    """中间件只读列表/详情（无 Create/Update schema：写接口已下线）。"""

    id: str
    name: str
    class_path: str  # 本地 Middleware 类导入路径
    config: dict[str, Any]  # 构造参数

    model_config = {"from_attributes": True}


# ── Skill ────────────────────────────────────────────────────────────────


class SkillCreate(BaseModel):
    """POST /api/skill：新建 Skill（content 为完整 SKILL.md 或纯正文）。"""

    name: str  # 全局唯一；亦作物化子目录名
    description: str = ""
    content: str  # 完整 SKILL.md 或正文（无 frontmatter 时服务端自动包装）
    config: dict[str, Any] = Field(default_factory=dict)
    status: str = "active"
    id: str | None = None


class SkillUpdate(BaseModel):
    """PATCH /api/skill/{id}。"""

    name: str | None = None
    description: str | None = None
    content: str | None = None
    config: dict[str, Any] | None = None
    status: str | None = None


class SkillOut(BaseModel):
    """Skill 列表/详情响应。"""

    id: str
    name: str
    description: str
    content: str
    files: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    status: str
    created_time: datetime
    updated_time: datetime

    model_config = {"from_attributes": True}


# ── Conversation / Chat ──────────────────────────────────────────────────


class ConversationCreate(BaseModel):
    """POST /api/conversation：基于已发布方法论开新会话。"""

    methodology_id: str  # 必须是 published 方法论（且归属当前用户）
    thread_id: str | None = None  # 可选指定；否则服务端生成（LangGraph 用）


class ConversationOut(BaseModel):
    """会话元信息响应（不含消息正文；消息在 checkpointer 里）。"""

    id: str
    thread_id: str  # 多轮状态隔离键
    user_id: str
    methodology_id: str
    methodology_version: int  # 创建时锁定的方法论版本
    created_time: datetime

    model_config = {"from_attributes": True}


class ChatRequest(BaseModel):
    """POST /api/chat：向已有会话发一条用户消息。"""

    thread_id: str
    message: str = Field(..., min_length=1, max_length=2_000_000)


class ChatResumeRequest(BaseModel):
    """POST /api/chat/resume：恢复 HITL 中断（批准/拒绝工具调用）。"""

    thread_id: str
    approve: bool = True  # True 批准继续；False 拒绝


class ChatMessageOut(BaseModel):
    """会话历史中的单条消息（从 checkpointer state 提取后返回前端）。"""

    role: str  # user | assistant | system | tool
    content: str
    name: str | None = None  # 如 tool / agent 名
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class ConversationMessagesOut(BaseModel):
    """GET .../messages：某 thread 的历史回放。"""

    thread_id: str
    methodology_id: str
    methodology_version: int
    messages: list[ChatMessageOut] = Field(default_factory=list)
    interrupted: bool = False  # 当前是否仍停在 HITL
    interrupt: list[dict[str, Any]] | None = None
