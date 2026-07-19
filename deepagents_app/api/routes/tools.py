"""Tool 注册 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from deepagents_app.api.schemas import ToolCreate, ToolOut, ToolUpdate
from deepagents_app.db.session import get_db
from deepagents_app.services import tools as tools_svc

router = APIRouter(tags=["tools"])


@router.get("/tool/list", response_model=list[ToolOut])
def list_tools(
    status: str | None = Query(None),
    db: Session = Depends(get_db),
):
    return tools_svc.list_tools(db, status=status)


@router.post("/tool", response_model=ToolOut)
def create_tool(body: ToolCreate, db: Session = Depends(get_db)):
    try:
        return tools_svc.create_tool(
            db,
            name=body.name,
            class_path=body.class_path,
            description=body.description,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            config=body.config,
            status=body.status,
            tool_id=body.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/tool/{tool_id}", response_model=ToolOut)
def get_tool(tool_id: str, db: Session = Depends(get_db)):
    row = tools_svc.get_tool(db, tool_id)
    if row is None:
        raise HTTPException(status_code=404, detail="工具不存在")
    return row


@router.patch("/tool/{tool_id}", response_model=ToolOut)
def update_tool(tool_id: str, body: ToolUpdate, db: Session = Depends(get_db)):
    try:
        return tools_svc.update_tool(
            db,
            tool_id,
            name=body.name,
            description=body.description,
            class_path=body.class_path,
            input_schema=body.input_schema,
            output_schema=body.output_schema,
            config=body.config,
            status=body.status,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.delete("/tool/{tool_id}")
def delete_tool(tool_id: str, db: Session = Depends(get_db)):
    try:
        tools_svc.delete_tool(db, tool_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True}
