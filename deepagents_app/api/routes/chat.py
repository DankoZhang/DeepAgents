#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   chat.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   chat.py

聊天 API
========

- ``POST /chat/stream``：SSE（meta / token / tool_* / todo / ping / done / error）
- ``POST /chat/resume/stream``：HITL 批准/拒绝

SSE 路由不注入 ``get_async_db``：组装在短事务内完成并关闭 Session，
流式输出期间不占用连接池。先 ``prepare_chat``，再抢流式槽位打开响应；
满载返回 429（冷编译不占槽）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse

from deepagents_app.api.schemas import ChatRequest, ChatResumeRequest
from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.config import get_settings
from deepagents_app.services.runtime.chat import (
    iter_chat_sse,
    iter_resume_sse,
    prepare_chat,
    validate_chat_message,
)
from deepagents_app.services.runtime.stream_limiter import (
    acquire_stream_slot,
    release_stream_slot,
)

router = APIRouter(tags=["chat"])

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


@router.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    user_id: str = Depends(require_user),
):
    """SSE：``meta`` → ``token``* → ``done`` | ``error``。"""
    settings = get_settings()
    # 先于 prepare_chat 校验，避免无效大消息触发 Agent 冷编译。
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
