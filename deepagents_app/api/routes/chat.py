"""
聊天 API
========

- ``POST /chat``：按 thread 发一轮消息
- ``POST /chat/stream``：SSE（meta / token / tool_* / todo / ping / done / error）
- ``POST /chat/resume`` / ``/chat/resume/stream``：HITL 批准/拒绝

SSE 路由不注入 ``get_async_db``：组装在短事务内完成并关闭 Session，
流式输出期间不占用连接池。先 ``prepare_chat``，再抢流式槽位打开响应；
满载返回 429（冷编译不占槽）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from deepagents_app.api.schemas import ChatRequest, ChatResponse, ChatResumeRequest
from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.config import get_settings
from deepagents_app.services.runtime.chat import (
    acquire_stream_slot,
    chat as run_chat,
    iter_chat_sse,
    iter_resume_sse,
    prepare_chat,
    release_stream_slot,
    resume_chat,
    validate_chat_message,
)

router = APIRouter(tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    user_id: str = Depends(require_user),
):
    """按会话绑定的方法论版本 invoke Agent（组装后释放 DB）。"""
    return ChatResponse(
        **(
            await run_chat(
                user_id=user_id,
                thread_id=body.thread_id,
                message=body.message,
            )
        )
    )


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    user_id: str = Depends(require_user),
):
    """SSE：``meta`` → ``token``* → ``done`` | ``error``。"""
    settings = get_settings()
    # 先于 prepare_chat 校验，避免无效大消息触发 Agent 冷编译。
    # iter_chat_sse 仍保留服务层校验，供非 HTTP 调用方使用。
    validate_chat_message(body.message, settings)
    # 冷编译在抢槽之前完成，避免占满并发槽后误报 429
    prepared = await prepare_chat(
        user_id=user_id, thread_id=body.thread_id, settings=settings
    )
    slot = await acquire_stream_slot(settings)

    async def event_gen():
        try:
            async for chunk in iter_chat_sse(
                user_id=user_id,
                thread_id=body.thread_id,
                message=body.message,
                prepared=prepared,
                stream_slot=slot,
            ):
                yield chunk
        finally:
            await release_stream_slot(slot)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.post("/chat/resume", response_model=ChatResponse)
async def chat_resume(
    body: ChatResumeRequest,
    user_id: str = Depends(require_user),
):
    """Human-in-the-loop：批准或拒绝中断后的工具调用。"""
    return ChatResponse(
        **(
            await resume_chat(
                user_id=user_id,
                thread_id=body.thread_id,
                approve=body.approve,
            )
        )
    )


@router.post("/chat/resume/stream")
async def chat_resume_stream(
    body: ChatResumeRequest,
    user_id: str = Depends(require_user),
):
    """HITL 恢复的 SSE 版本。"""
    settings = get_settings()
    prepared = await prepare_chat(
        user_id=user_id, thread_id=body.thread_id, settings=settings
    )
    slot = await acquire_stream_slot(settings)

    async def event_gen():
        try:
            async for chunk in iter_resume_sse(
                user_id=user_id,
                thread_id=body.thread_id,
                approve=body.approve,
                prepared=prepared,
                stream_slot=slot,
            ):
                yield chunk
        finally:
            await release_stream_slot(slot)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )
