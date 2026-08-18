#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   llm_models.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   llm_models.py

大模型目录
==========

- CRUD：用户维护可用模型（provider / base_url / 超参）
- 连通性测试：不落库，仅验证配置能否发起调用
- 变更后 bump 引用该模型的方法论，旧会话靠快照 llm 重建
- ``is_default``：同一用户至多一个默认模型；创建 Agent 未指定模型时使用
- 组装时 ``resolve_model_spec_for_agent``：快照 llm → 目录 → Settings 默认；
  有目录 ``model_id`` 时一律校验 live ``status=active`` 且已配置 API Key
  （禁止串用进程级 ``OPENAI_API_KEY``）
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import Settings, get_settings
from deepagents_app.constants import ALLOWED_PROVIDERS
from deepagents_app.crypto import decrypt_secret, encrypt_secret
from deepagents_app.db.models import AgentDefinition, ModelDefinition
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.llm import build_chat_model, model_spec_from_row
from deepagents_app.ownership import default_model_id_for_user
from deepagents_app.utils.text import normalize_message_content
from deepagents_app.utils.url_safety import assert_safe_http_url
from deepagents_app.services.catalog.crud_helpers import (
    ensure_unique_owned_name,
    get_owned,
    resolve_resource_id,
)

logger = logging.getLogger(__name__)


async def list_models(
    db: AsyncSession,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[ModelDefinition], int, str | None]:
    """列出当前用户的模型目录；可按 status 过滤。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(ModelDefinition)
        .where(ModelDefinition.owner_user_id == owner_user_id)
        .order_by(ModelDefinition.name, ModelDefinition.id)
    )
    if status:
        stmt = stmt.where(ModelDefinition.status == status)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        cursor=cursor,
        sort_column=ModelDefinition.name,
        id_column=ModelDefinition.id,
        sort_attr="name",
    )


async def get_model(
    db: AsyncSession, model_id: str, *, owner_user_id: str
) -> ModelDefinition | None:
    """按主键取模型；不属于当前用户则视为不存在。"""

    return await get_owned(db, ModelDefinition, model_id, owner_user_id=owner_user_id)


async def get_default_model(
    db: AsyncSession, *, owner_user_id: str
) -> ModelDefinition | None:
    """当前用户标记为默认的模型；同一用户至多一条。"""

    return (
        await db.scalars(
            select(ModelDefinition).where(
                ModelDefinition.owner_user_id == owner_user_id,
                ModelDefinition.is_default.is_(True),
            )
        )
    ).one_or_none()


async def _promote_exclusive_default(db: AsyncSession, row: ModelDefinition) -> None:
    """将 row 设为该用户唯一默认模型，其余自动取消。"""

    others = (
        await db.scalars(
            select(ModelDefinition).where(
                ModelDefinition.owner_user_id == row.owner_user_id,
                ModelDefinition.id != row.id,
                ModelDefinition.is_default.is_(True),
            )
        )
    ).all()
    now = datetime.now(timezone.utc)
    for other in others:
        other.is_default = False
        other.updated_time = now
    if others:
        # 先落库取消旧默认，避免与随后的新默认同时为 True 触发唯一索引
        await db.flush()
    row.is_default = True


async def create_model(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    provider: str,
    model_name: str,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = 0.2,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    config: dict[str, Any] | None = None,
    status: str = "active",
    is_default: bool = False,
    model_id: str | None = None,
) -> ModelDefinition:
    """创建模型目录项；同用户下 name 唯一。新建默认非默认模型。"""

    _validate_provider(provider)
    if base_url:
        base_url = assert_safe_http_url(
            base_url,
            label="base_url",
            allow_private=bool(get_settings().auth_disabled),
        )
    await ensure_unique_owned_name(
        db,
        ModelDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="模型配置",
    )

    row = ModelDefinition(
        id=resolve_resource_id(model_id, prefix="model_", label="model id"),
        owner_user_id=owner_user_id,
        name=name,
        provider=provider,
        model_name=model_name,
        api_key=encrypt_secret(api_key),
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        timeout=timeout,
        config=dict(config or {}),
        status=status,
        is_default=False,
    )
    db.add(row)
    await db.flush()
    if is_default:
        await _promote_exclusive_default(db, row)
        await db.flush()
    return row


async def update_model(
    db: AsyncSession,
    model_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    provider: str | None = None,
    model_name: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    is_default: bool | None = None,
    bump_related: bool = True,
) -> ModelDefinition:
    """
    更新模型；字段为 None 表示不改。

    ``api_key`` 有值则加密覆盖；``bump_related=True`` 时升版所有引用该模型的方法论。
    """
    row = await get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")

    if name is not None and name != row.name:
        await ensure_unique_owned_name(
            db,
            ModelDefinition,
            owner_user_id=owner_user_id,
            name=name,
            exclude_id=model_id,
            label="模型配置",
        )
        row.name = name

    if provider is not None:
        row.provider = provider
    if model_name is not None:
        row.model_name = model_name
    if api_key is not None:
        row.api_key = encrypt_secret(api_key or None)
    if base_url is not None:
        if base_url:
            base_url = assert_safe_http_url(
                base_url,
                label="base_url",
                allow_private=bool(get_settings().auth_disabled),
            )
        row.base_url = base_url or None
    if temperature is not None:
        row.temperature = temperature
    if top_p is not None:
        row.top_p = top_p
    if max_tokens is not None:
        row.max_tokens = max_tokens
    if timeout is not None:
        row.timeout = timeout
    if config is not None:
        # 浅合并：保留未提交的既有键
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if status is not None:
        row.status = status
    if is_default is True:
        await _promote_exclusive_default(db, row)
    elif is_default is False:
        row.is_default = False

    _validate_provider(row.provider)
    row.updated_time = datetime.now(timezone.utc)
    await db.flush()

    if bump_related:
        from deepagents_app.services.versioning.revisions import (
            bump_methodologies_using_resource,
        )

        await bump_methodologies_using_resource(
            db, kind="model", resource_id=model_id
        )
    return row


async def delete_model(
    db: AsyncSession,
    model_id: str,
    *,
    owner_user_id: str,
) -> None:
    """删除模型；仍被 Agent 引用时拒绝，需先改绑或删 Agent。"""
    row = await get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")

    agents = list(
        await db.scalars(
            select(AgentDefinition).where(
                AgentDefinition.model_id == model_id,
                AgentDefinition.owner_user_id == owner_user_id,
            )
        )
    )
    if agents:
        names = ", ".join(a.name for a in agents[:5])
        more = "" if len(agents) <= 5 else f" 等 {len(agents)} 个"
        raise BusinessError(f"模型仍被 Agent 引用，无法删除：{names}{more}")

    was_default = row.is_default
    owner_user_id = row.owner_user_id
    # 无 Agent 引用：无需 bump / 清缓存
    await db.delete(row)
    await db.flush()
    if was_default:
        replacement = (
            await db.scalars(
                select(ModelDefinition)
                .where(
                    ModelDefinition.owner_user_id == owner_user_id,
                    ModelDefinition.id != model_id,
                )
                .order_by(
                    (ModelDefinition.status == "active").desc(),
                    ModelDefinition.created_time,
                    ModelDefinition.id,
                )
            )
        ).first()
        if replacement is not None:
            replacement.is_default = True
            replacement.updated_time = datetime.now(timezone.utc)
            await db.flush()


async def test_model_connectivity(
    *,
    settings: Settings | None = None,
    provider: str,
    model_name: str,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = 0.0,
    top_p: float | None = None,
    max_tokens: int | None = 16,
    timeout: float | None = 30.0,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """对给定配置发起一次极简调用，验证连通性（不写库）。"""
    settings = settings or get_settings()
    _validate_provider(provider)
    if not (api_key and str(api_key).strip()):
        return {
            "ok": False,
            "message": "未提供 API Key，拒绝使用进程级默认密钥试连",
            "reply_preview": None,
        }
    if base_url:
        # 生产拦截 SSRF；本地 AUTH_DISABLED 允许 Ollama 等私网端点
        base_url = assert_safe_http_url(
            base_url,
            label="base_url",
            allow_private=bool(settings.auth_disabled),
        )
    try:
        chat = build_chat_model(
            settings,
            provider=provider,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            temperature=0.0 if temperature is None else temperature,
            top_p=top_p,
            max_tokens=max_tokens if max_tokens is not None else 16,
            timeout=timeout if timeout is not None else 30.0,
            extra=config,
        )
        result = await chat.ainvoke("ping")
        content = getattr(result, "content", None)
        text = normalize_message_content(content)
        return {
            "ok": True,
            "message": "连通性测试成功",
            "reply_preview": text[:200],
        }
    except BusinessError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型连通性测试失败：%s", exc)
        return {
            "ok": False,
            "message": "连通性测试失败",
            "reply_preview": None,
        }


async def test_model_by_id(
    db: AsyncSession, model_id: str, *, owner_user_id: str
) -> dict[str, Any]:
    """按目录 id 测连通性；disabled 模型直接返回失败说明。"""
    row = await get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")
    if row.status != "active":
        return {
            "ok": False,
            "message": f"模型状态为 {row.status}，未测试",
            "reply_preview": None,
        }
    return await test_model_connectivity(
        provider=row.provider,
        model_name=row.model_name,
        api_key=_decrypt_api_key(
            row.api_key, context=f"模型 {model_id} 的 api_key 解密失败"
        ),
        base_url=row.base_url,
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=row.max_tokens if row.max_tokens is not None else 16,
        timeout=row.timeout if row.timeout is not None else 30.0,
        config=dict(row.config or {}),
    )


# ── 组装解析：快照/目录 → 可调用的 ChatModel ──────────────────────────


def serialize_model(
    row: ModelDefinition | None,
) -> dict[str, Any] | None:
    """
    模型配置（live 组装与快照共用）。

    不含 api_key：重建时按 model_id 从 live 目录回填密钥。
    """
    if row is None:
        return None
    return {
        "model_id": row.id,
        "name": row.name,
        "provider": row.provider,
        "model_name": row.model_name,
        "base_url": row.base_url,
        "temperature": row.temperature,
        "top_p": row.top_p,
        "max_tokens": row.max_tokens,
        "timeout": row.timeout,
        "extra": dict(row.config or {}),
    }


def _decrypt_api_key(stored: str | None, *, context: str) -> str | None:
    """解密失败转为可读 BusinessError，避免组装路径变成 500。"""
    try:
        return decrypt_secret(stored)
    except ValueError as exc:
        raise BusinessError(f"{context}：{exc}") from exc


def _require_catalog_api_key(api_key: str | None, *, label: str) -> str:
    """目录模型必须自带密钥，禁止回退进程级 OPENAI_API_KEY。"""
    key = (api_key or "").strip()
    if not key:
        raise BusinessError(f"模型未配置 API Key：{label}")
    return key


async def resolve_model_spec_for_agent(
    db: AsyncSession,
    *,
    owner_user_id: str,
    model_id: str | None,
    snapshot_llm: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    组装 Agent 时解析最终模型 spec。

    优先级：快照 llm（密钥一律按 model_id 从 live 回填）> model_id 目录；
    凡能解析到目录 ``model_id`` 的，一律读 live，要求 ``status=active``
    且已配置 API Key（禁止因内嵌 llm / 缺密钥而串用进程级默认密钥）。
    因此禁用或改密 live 模型会立刻影响仍锁定该 model_id 的旧会话。
    皆无则返回 None（上层回退 Settings/.env，仅未绑定目录模型时）。
    """
    mid: str | None = None
    if snapshot_llm and snapshot_llm.get("model_id"):
        mid = str(snapshot_llm["model_id"])
    elif model_id:
        mid = str(model_id)

    live: ModelDefinition | None = None
    if mid:
        live = await get_model(db, mid, owner_user_id=owner_user_id)
        if live is None:
            raise NotFoundError(f"Agent 绑定的模型不存在：{mid}")
        if live.status != "active":
            raise BusinessError(f"模型已禁用：{live.name} ({mid})")

    if snapshot_llm:
        if live is None:
            raise BusinessError(
                "快照模型缺少可校验的 model_id，拒绝回退进程级默认密钥"
            )
        spec = dict(snapshot_llm)
        # 快照不存密钥；始终从 live 回填，且必须非空
        spec["api_key"] = _require_catalog_api_key(
            _decrypt_api_key(
                live.api_key, context=f"模型 {mid} 的 api_key 解密失败"
            ),
            label=f"{live.name} ({mid})",
        )
        if not spec.get("base_url"):
            spec["base_url"] = live.base_url
        return spec

    if live is not None:
        spec = model_spec_from_row(live)
        raw_key = spec.get("api_key")
        spec["api_key"] = _require_catalog_api_key(
            raw_key if isinstance(raw_key, str) else None,
            label=f"{live.name} ({mid})",
        )
        return spec

    return None


async def ensure_default_model_from_settings(
    db: AsyncSession,
    settings: Settings | None = None,
    *,
    owner_user_id: str,
    model_id: str | None = None,
) -> ModelDefinition:
    """幂等：用 Settings/.env 种子一条该用户的默认模型目录项。"""
    settings = settings or get_settings()
    resolved_id = (
        model_id if model_id is not None else default_model_id_for_user(owner_user_id)
    )
    existing = await get_model(db, resolved_id, owner_user_id=owner_user_id)
    if existing is not None:
        return existing
    return await create_model(
        db,
        owner_user_id=owner_user_id,
        model_id=resolved_id,
        name="默认模型",
        provider=settings.model_provider,
        model_name=settings.model_name,
        api_key=settings.openai_api_key
        if settings.model_provider != "anthropic"
        else settings.anthropic_api_key,
        base_url=settings.openai_base_url,
        temperature=0.2,
        status="active",
        is_default=True,
    )


def _validate_provider(provider: str) -> None:
    """校验 provider 枚举。"""
    if provider not in ALLOWED_PROVIDERS:
        raise BusinessError(
            f"不支持的 provider：{provider}，可选：{', '.join(sorted(ALLOWED_PROVIDERS))}"
        )
