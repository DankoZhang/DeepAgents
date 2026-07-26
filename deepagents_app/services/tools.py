"""Tool 注册管理：内置只读；API 仅可新增/编辑 MCP。"""

# 推迟注解求值，便于类型提示里写尚未完全定义的前向引用
from __future__ import annotations

# 生成工具主键后缀（未显式传 tool_id 时）
import uuid
# Any：MCP config / schema 等任意 JSON 结构
from typing import Any

# SQLAlchemy 会话：执行查询与事务内 flush
from sqlalchemy.orm import Session

# ORM 模型：对应 tool_definition 表
from deepagents_app.db.models import ToolDefinition
# 工具变更后清空 Compiled Agent 缓存，避免继续用旧工具列表
from deepagents_app.services.agent_factory import invalidate_agent_cache


def list_tools(
    db: Session,  # 当前请求/调用方传入的 DB 会话
    *,  # 其后参数必须关键字传入，避免位置参数搞混
    status: str | None = None,  # 可选过滤：active / disabled
    tool_type: str | None = None,  # 可选过滤：builtin / mcp
) -> list[ToolDefinition]:
    # 查全部工具，先按类型再按名称排序，列表展示更稳定
    q = db.query(ToolDefinition).order_by(ToolDefinition.tool_type, ToolDefinition.name)
    # 若指定状态则追加 WHERE status = ...
    if status:
        q = q.filter(ToolDefinition.status == status)
    # 若指定类型则追加 WHERE tool_type = ...
    if tool_type:
        q = q.filter(ToolDefinition.tool_type == tool_type)
    # 执行查询并返回 ORM 对象列表
    return q.all()


def get_tool(db: Session, tool_id: str) -> ToolDefinition | None:
    # 按主键取单行；不存在返回 None（由路由层转 404）
    return db.get(ToolDefinition, tool_id)


def create_builtin_tool(
    db: Session,
    *,
    name: str,  # 全局唯一工具名
    class_path: str,  # 可导入路径 module:attr，运行时动态加载
    description: str = "",  # 给人看的说明
    input_schema: dict[str, Any] | None = None,  # 可选入参 JSON Schema
    output_schema: dict[str, Any] | None = None,  # 可选出参 JSON Schema
    config: dict[str, Any] | None = None,  # 内置工具扩展配置（如 instantiate）
    status: str = "active",  # 默认可用
    tool_id: str | None = None,  # 可选固定 id（种子数据用）
) -> ToolDefinition:
    """种子/内部用：写入 builtin 工具（不走对外「仅 MCP」创建 API）。"""
    # 名称全局唯一：已存在则拒绝，避免覆盖种子或冲突
    if db.query(ToolDefinition).filter(ToolDefinition.name == name).one_or_none():
        raise ValueError(f"工具名已存在：{name}")
    # 构造 ORM 行：类型固定为 builtin，必须带 class_path
    row = ToolDefinition(
        # 有指定 id 用指定值，否则生成 tool_<12位hex>
        id=tool_id or f"tool_{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        tool_type="builtin",  # 标记为内置，前端不可新建此类
        class_path=class_path,  # 运行时 importlib 加载目标
        input_schema=input_schema,
        output_schema=output_schema,
        # None 时落空 dict，避免 JSON 列存 NULL
        config=config or {},
        status=status,
    )
    # 加入当前 Session（尚未 commit）
    db.add(row)
    # flush：拿到 DB 约束校验结果，并让同事务后续查询可见
    db.flush()
    # 返回刚创建的行（仍属当前事务）
    return row


def create_mcp_tool(
    db: Session,
    *,
    name: str,  # MCP Server 在系统中的显示/引用名
    mcp_config: dict[str, Any],  # transport/command/url 等连接配置
    description: str = "",
    status: str = "active",
    tool_id: str | None = None,
) -> ToolDefinition:
    """前端/API：仅创建 MCP 工具。"""
    # 与内置工具共用 name 唯一约束
    if db.query(ToolDefinition).filter(ToolDefinition.name == name).one_or_none():
        raise ValueError(f"工具名已存在：{name}")
    # MCP 行：无 class_path，执行体在 config 描述的远程 Server
    row = ToolDefinition(
        id=tool_id or f"tool_{uuid.uuid4().hex[:12]}",
        name=name,
        description=description,
        tool_type="mcp",  # 运行时走 MultiServerMCPClient 展开
        class_path=None,  # MCP 不依赖本地 Python 路径
        # 拷贝一份，避免调用方后续改动原 dict 影响已挂到 Session 的对象
        config=dict(mcp_config),
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def update_tool(
    db: Session,
    tool_id: str,  # 要更新的主键
    *,
    name: str | None = None,  # None 表示本字段不改
    description: str | None = None,
    mcp_config: dict[str, Any] | None = None,  # 仅 MCP 可改连接配置
    status: str | None = None,  # builtin 允许只改这个
) -> ToolDefinition:
    # 按 id 加载；不存在则上层转 404
    row = db.get(ToolDefinition, tool_id)
    if row is None:
        raise LookupError(f"工具不存在：{tool_id}")
    # 内置工具：禁止改名称/描述/连接，只允许停用类 status 变更
    if row.tool_type == "builtin":
        if name is not None or description is not None or mcp_config is not None:
            raise ValueError("内置工具不可修改名称/描述/连接配置，仅可更新 status")
    # 选择性更新：只有显式传入的字段才写入
    if name is not None:
        row.name = name
    if description is not None:
        row.description = description
    if mcp_config is not None:
        # 双保险：非 mcp 行即使绕过上面检查也不能写连接配置
        if row.tool_type != "mcp":
            raise ValueError("仅 MCP 工具可更新连接配置")
        row.config = dict(mcp_config)
    if status is not None:
        row.status = status
    # 工具元信息变了，已编译 Agent 里的工具列表可能过时
    invalidate_agent_cache()
    # 把脏对象刷到 DB（仍由调用方/Depends 负责 commit）
    db.flush()
    return row


def delete_tool(db: Session, tool_id: str) -> None:
    # 加载目标行
    row = db.get(ToolDefinition, tool_id)
    if row is None:
        raise LookupError(f"工具不存在：{tool_id}")
    # 内置工具禁止物理删除，引导改为 disabled
    if row.tool_type == "builtin":
        raise ValueError("内置工具不可删除，请改为 disabled")
    # 删除前失效缓存，避免 Agent 仍引用已删 MCP
    invalidate_agent_cache()
    # 从 Session 标记删除（级联会清 agent_tool 关系，取决于 FK ondelete）
    db.delete(row)
    # 立即执行 DELETE，暴露约束错误
    db.flush()
