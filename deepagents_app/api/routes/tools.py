"""
Tool 注册 API
=============

- 列表 / 详情：builtin + mcp
- 新建 / 删除：仅 MCP
- 内置工具不可改执行体、不可删（可 disabled）
"""

# 推迟注解求值
from __future__ import annotations

# FastAPI：路由、依赖注入、HTTP 异常、查询参数声明
from fastapi import APIRouter, Depends, HTTPException, Query
# 请求级 DB Session 类型
from sqlalchemy.orm import Session

# 请求/响应 Pydantic 模型（校验 body、序列化输出）
from deepagents_app.api.schemas import ToolCreate, ToolOut, ToolUpdate
# 每个请求 yield 一个 Session，结束时 commit/rollback
from deepagents_app.db.session import get_db
# 业务层：真正写库 / 查库的逻辑
from deepagents_app.services import tools as tools_svc

# 本模块路由集合；OpenAPI 里归到 tools 分组
router = APIRouter(tags=["tools"])


@router.get("/tool/list", response_model=list[ToolOut])  # GET 列表；响应校验为 ToolOut 数组
def list_tools(
    # Query：?status=active|disabled，缺省不过滤
    status: str | None = Query(None, description="active | disabled"),
    # Query：?tool_type=builtin|mcp，缺省不过滤
    tool_type: str | None = Query(None, description="builtin | mcp"),
    # Depends：自动注入并在请求结束时关闭/提交 Session
    db: Session = Depends(get_db),
):
    # 委托 service 层查询并原样返回（FastAPI 再按 ToolOut 序列化）
    return tools_svc.list_tools(db, status=status, tool_type=tool_type)


@router.post("/tool", response_model=ToolOut)  # 创建工具；成功返回新建行
def create_tool(body: ToolCreate, db: Session = Depends(get_db)):
    """仅创建 MCP 工具（body.mcp 为连接配置；schema 层已禁止 class_path 式创建）。"""
    try:
        # 调 service：写入 tool_type=mcp 的 ToolDefinition
        return tools_svc.create_mcp_tool(
            db,
            name=body.name,  # 工具显示名 / 唯一名
            description=body.description,  # 可选说明
            # Pydantic 模型转普通 dict，存入 config JSON 列
            mcp_config=body.mcp.model_dump(),
            status=body.status,  # 默认 active
            tool_id=body.id,  # 可选客户端指定主键
        )
    except ValueError as exc:
        # 业务校验失败（如重名）→ 400，保留异常链便于日志
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tool/{tool_id}", response_model=ToolOut)  # 路径参数取单个工具
def get_tool(tool_id: str, db: Session = Depends(get_db)):
    # 按主键查；可能为 None
    row = tools_svc.get_tool(db, tool_id)
    if row is None:
        # 不存在统一 404
        raise HTTPException(status_code=404, detail="工具不存在")
    # 存在则返回 ORM 行，由 response_model 转 JSON
    return row


@router.patch("/tool/{tool_id}", response_model=ToolOut)  # 部分更新
def update_tool(tool_id: str, body: ToolUpdate, db: Session = Depends(get_db)):
    try:
        return tools_svc.update_tool(
            db,
            tool_id,  # 路径上的目标 id
            name=body.name,  # 未传则为 None，service 跳过该字段
            description=body.description,
            # 仅当 body 带了 mcp 时才序列化；否则 None 表示不改连接配置
            mcp_config=body.mcp.model_dump() if body.mcp else None,
            status=body.status,
        )
    except LookupError as exc:
        # service 找不到行 → 404
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        # 如「内置不可改」「仅 MCP 可改 config」→ 400
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/tool/{tool_id}")  # 删除（仅 MCP）；无 response_model，返回简单 dict
def delete_tool(tool_id: str, db: Session = Depends(get_db)):
    try:
        # service：builtin 会抛 ValueError；不存在抛 LookupError
        tools_svc.delete_tool(db, tool_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # 成功统一返回 ok，便于前端判断
    return {"ok": True}
