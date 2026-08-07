"""
大模型目录
==========

- CRUD：用户维护可用模型（provider / base_url / 超参）
- 连通性测试：不落库，仅验证配置能否发起调用
- 变更后 bump 引用该模型的方法论，旧会话靠快照 llm 重建
- 组装时 ``resolve_model_spec_for_agent``：快照 llm → 目录 → Settings 默认
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import Settings, get_settings
from deepagents_app.crypto import decrypt_secret, encrypt_secret, secret_is_present
from deepagents_app.db.models import AgentDefinition, ModelDefinition
from deepagents_app.llm import build_chat_model, model_spec_from_row
from deepagents_app.ownership import default_model_id_for_user, validate_resource_id
from deepagents_app.utils.text import normalize_message_content

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS = frozenset({"openai", "anthropic", "openai_compatible"})


def list_models(
    db: Session,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[ModelDefinition], int]:
    """列出当前用户的模型目录；可按 status 过滤。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(ModelDefinition)
        .filter(ModelDefinition.owner_user_id == owner_user_id)
        .order_by(ModelDefinition.name)
    )
    if status:
        q = q.filter(ModelDefinition.status == status)
    return paginate_query(q, limit=limit, offset=offset)


def get_model(
    db: Session, model_id: str, *, owner_user_id: str
) -> ModelDefinition | None:
    """按主键取模型；不属于当前用户则视为不存在。"""
    from deepagents_app.services.crud_helpers import get_owned

    return get_owned(db, ModelDefinition, model_id, owner_user_id=owner_user_id)


def create_model(
    db: Session,
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
    model_id: str | None = None,
) -> ModelDefinition:
    """创建模型目录项；同用户下 name 唯一。"""
    from deepagents_app.services.crud_helpers import ensure_unique_owned_name

    _validate_provider(provider, base_url=base_url)
    ensure_unique_owned_name(
        db,
        ModelDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="模型配置",
    )

    row = ModelDefinition(
        id=_resolve_model_create_id(model_id),
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
    )
    db.add(row)
    db.flush()
    return row


def update_model(
    db: Session,
    model_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    provider: str | None = None,
    model_name: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    base_url: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> ModelDefinition:
    """
    更新模型；字段为 None 表示不改。

    ``clear_api_key=True`` 显式清空密钥（与传空字符串区分）。
    ``bump_related=True`` 时升版所有引用该模型的方法论。
    """
    row = get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")

    if name is not None and name != row.name:
        from deepagents_app.services.crud_helpers import ensure_unique_owned_name

        ensure_unique_owned_name(
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
    if clear_api_key:
        row.api_key = None
    elif api_key is not None:
        row.api_key = encrypt_secret(api_key or None)
    if base_url is not None:
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

    _validate_provider(row.provider, base_url=row.base_url)
    row.updated_time = datetime.now(timezone.utc)
    db.flush()

    if bump_related:
        from deepagents_app.services.revisions import bump_methodologies_using_model

        bump_methodologies_using_model(db, model_id)
    return row


def delete_model(
    db: Session,
    model_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,  # noqa: ARG001 — 保留签名；无引用时无需 bump
) -> None:
    """删除模型；仍被 Agent 引用时拒绝，需先改绑或删 Agent。"""
    row = get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")

    agents = (
        db.query(AgentDefinition)
        .filter(
            AgentDefinition.model_id == model_id,
            AgentDefinition.owner_user_id == owner_user_id,
        )
        .all()
    )
    if agents:
        names = ", ".join(a.name for a in agents[:5])
        more = "" if len(agents) <= 5 else f" 等 {len(agents)} 个"
        raise BusinessError(f"模型仍被 Agent 引用，无法删除：{names}{more}")

    # 无 Agent 引用：无需 bump / 清缓存
    db.delete(row)
    db.flush()


def test_model_connectivity(
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
    _validate_provider(provider, base_url=base_url)
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
        result = chat.invoke("ping")
        content = getattr(result, "content", None)
        text = normalize_message_content(content)
        return {
            "ok": True,
            "message": "连通性测试成功",
            "reply_preview": text[:200],
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("模型连通性测试失败：%s", exc)
        return {
            "ok": False,
            "message": str(exc),
            "reply_preview": None,
        }


def test_model_by_id(
    db: Session, model_id: str, *, owner_user_id: str
) -> dict[str, Any]:
    """按目录 id 测连通性；disabled 模型直接返回失败说明。"""
    row = get_model(db, model_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"模型不存在：{model_id}")
    if row.status != "active":
        return {
            "ok": False,
            "message": f"模型状态为 {row.status}，未测试",
            "reply_preview": None,
        }
    return test_model_connectivity(
        provider=row.provider,
        model_name=row.model_name,
        api_key=decrypt_secret(row.api_key),
        base_url=row.base_url,
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=row.max_tokens if row.max_tokens is not None else 16,
        timeout=row.timeout if row.timeout is not None else 30.0,
        config=dict(row.config or {}),
    )


# ── 组装解析：快照/目录 → 可调用的 ChatModel ──────────────────────────


def serialize_model_for_snapshot(
    row: ModelDefinition | None,
    *,
    include_secrets: bool = False,
) -> dict[str, Any] | None:
    """
    写入方法论快照的模型配置。

    默认不含 api_key：重建时按 model_id 从 live 目录回填密钥。
    """
    if row is None:
        return None
    payload = {
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
    if include_secrets:
        payload["api_key"] = decrypt_secret(row.api_key)
    return payload


def resolve_model_spec_for_agent(
    db: Session,
    *,
    owner_user_id: str,
    model_id: str | None,
    snapshot_llm: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    组装 Agent 时解析最终模型 spec。

    优先级：快照 llm（缺 api_key 则按 model_id 回填并解密）> model_id 目录；
    皆无则返回 None（上层回退 Settings/.env）。
    """
    if snapshot_llm:
        spec = dict(snapshot_llm)
        mid = spec.get("model_id")
        # 快照通常不存密钥，组装时从 live 目录补回
        if mid and not spec.get("api_key"):
            live = get_model(db, str(mid), owner_user_id=owner_user_id)
            if live is not None:
                spec["api_key"] = decrypt_secret(live.api_key)
                if not spec.get("base_url"):
                    spec["base_url"] = live.base_url
        elif spec.get("api_key"):
            try:
                spec["api_key"] = decrypt_secret(str(spec["api_key"]))
            except ValueError:
                pass
        return spec

    if model_id:
        row = get_model(db, model_id, owner_user_id=owner_user_id)
        if row is None:
            raise NotFoundError(f"Agent 绑定的模型不存在：{model_id}")
        if row.status != "active":
            raise BusinessError(f"模型已禁用：{row.name} ({model_id})")
        return model_spec_from_row(row)

    return None


def ensure_default_model_from_settings(
    db: Session,
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
    existing = get_model(db, resolved_id, owner_user_id=owner_user_id)
    if existing is not None:
        return existing
    return create_model(
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
    )


def _validate_provider(provider: str, *, base_url: str | None) -> None:
    """校验 provider 枚举；base_url 参数保留供后续扩展校验。"""
    if provider not in ALLOWED_PROVIDERS:
        raise BusinessError(
            f"不支持的 provider：{provider}，可选：{', '.join(sorted(ALLOWED_PROVIDERS))}"
        )


def _resolve_model_create_id(model_id: str | None) -> str:
    resolved = model_id or f"model_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="model id")
