"""
用户配置引导 API
================

显式触发按用户幂等种子（默认模型 / 工具 / demo 方法论等）。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.auth import get_current_user_id as require_user
from deepagents_app.db.seed import ensure_user_bootstrap
from deepagents_app.db.session import get_async_db
from deepagents_app.ownership import demo_methodology_id_for_user

router = APIRouter(tags=["bootstrap"])


@router.post("/bootstrap")
async def bootstrap_user(
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(require_user),
):
    """幂等：为当前用户准备默认配置。"""
    await ensure_user_bootstrap(db, user_id)
    return {
        "ok": True,
        "user_id": user_id,
        "demo_methodology_id": demo_methodology_id_for_user(user_id),
    }
