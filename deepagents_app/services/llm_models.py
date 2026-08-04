"""大模型目录 CRUD、连通性测试、变更后 bump 相关方法论。"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.config import Settings, get_settings
from deepagents_app.db.models import AgentDefinition, ModelDefinition, MethodologyAgent
from deepagents_app.models import build_chat_model, model_spec_from_row

logger = logging.getLogger(__name__)

ALLOWED_PROVIDERS = frozenset({"openai", "anthropic", "openai_compatible"})


def list_models(
    db: Session,
    *,
    status: str | None = None,
) -> list[ModelDefinition]:
    q = db.query(ModelDefinition).order_by(ModelDefinition.name)
    if status:
        q = q.filter(ModelDefinition.status == status)
    return q.all()


def get_model(db: Session, model_id: str) -> ModelDefinition | None:
    return db.get(ModelDefinition, model_id)


def create_model(
    db: Session,
    *,
    name: str,
    provider: str,
    model_name: str,
    api_key: str | None = None,
    base_url: str | None = None,
    temperature: float | None = 0.2,
    top_p: float | None = None,
    max_tokens: int | None = None,
    context_length: int | None = None,
    timeout: float | None = None,
    config: dict[str, Any] | None = None,
    status: str = "active",
    model_id: str | None = None,
) -> ModelDefinition:
    _validate_provider(provider, base_url=base_url)
    existing = (
        db.query(ModelDefinition).filter(ModelDefinition.name == name).one_or_none()
    )
    if existing is not None:
        raise ValueError(f"已存在同名模型配置：{name}")

    row = ModelDefinition(
        id=model_id or f"model_{uuid.uuid4().hex[:12]}",
        name=name,
        provider=provider,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        top_p=top_p,
        max_tokens=max_tokens,
        context_length=context_length,
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
    name: str | None = None,
    provider: str | None = None,
    model_name: str | None = None,
    api_key: str | None = None,
    clear_api_key: bool = False,
    base_url: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    context_length: int | None = None,
    timeout: float | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> ModelDefinition:
    row = get_model(db, model_id)
    if row is None:
        raise LookupError(f"模型不存在：{model_id}")

    if name is not None and name != row.name:
        clash = (
            db.query(ModelDefinition)
            .filter(ModelDefinition.name == name, ModelDefinition.id != model_id)
            .one_or_none()
        )
        if clash is not None:
            raise ValueError(f"已存在同名模型配置：{name}")
        row.name = name

    if provider is not None:
        row.provider = provider
    if model_name is not None:
        row.model_name = model_name
    if clear_api_key:
        row.api_key = None
    elif api_key is not None:
        row.api_key = api_key or None
    if base_url is not None:
        row.base_url = base_url or None
    if temperature is not None:
        row.temperature = temperature
    if top_p is not None:
        row.top_p = top_p
    if max_tokens is not None:
        row.max_tokens = max_tokens
    if context_length is not None:
        row.context_length = context_length
    if timeout is not None:
        row.timeout = timeout
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if status is not None:
        row.status = status

    _validate_provider(row.provider, base_url=row.base_url)
    row.updated_time = datetime.now(timezone.utc)
    db.flush()

    if bump_related:
        _bump_methodologies_using_model(db, model_id)
    return row


def delete_model(db: Session, model_id: str, *, bump_related: bool = True) -> None:
    row = get_model(db, model_id)
    if row is None:
        raise LookupError(f"模型不存在：{model_id}")

    agents = (
        db.query(AgentDefinition).filter(AgentDefinition.model_id == model_id).all()
    )
    if agents:
        names = ", ".join(a.name for a in agents[:5])
        more = "" if len(agents) <= 5 else f" 等 {len(agents)} 个"
        raise ValueError(f"模型仍被 Agent 引用，无法删除：{names}{more}")

    if bump_related:
        from deepagents_app.services.agent_factory import invalidate_agent_cache

        invalidate_agent_cache()
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
    """对给定配置发起一次极简调用，验证连通性。"""
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
        if isinstance(content, list):
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in content
            )
        else:
            text = "" if content is None else str(content)
        return {
            "ok": True,
            "message": "连通性测试成功",
            "reply_preview": text[:200],
        }
    except Exception as exc:  # noqa: BLE001 — 测试接口需把错误回给前端
        logger.warning("模型连通性测试失败：%s", exc)
        return {
            "ok": False,
            "message": str(exc),
            "reply_preview": None,
        }


def test_model_by_id(db: Session, model_id: str) -> dict[str, Any]:
    row = get_model(db, model_id)
    if row is None:
        raise LookupError(f"模型不存在：{model_id}")
    if row.status != "active":
        return {"ok": False, "message": f"模型状态为 {row.status}，未测试", "reply_preview": None}
    return test_model_connectivity(
        provider=row.provider,
        model_name=row.model_name,
        api_key=row.api_key,
        base_url=row.base_url,
        temperature=row.temperature,
        top_p=row.top_p,
        max_tokens=row.max_tokens if row.max_tokens is not None else 16,
        timeout=row.timeout if row.timeout is not None else 30.0,
        config=dict(row.config or {}),
    )


def serialize_model_for_snapshot(
    row: ModelDefinition | None,
    *,
    include_secrets: bool = False,
) -> dict[str, Any] | None:
    """写入方法论快照的模型配置（默认不含 api_key，重建时按 model_id 回填）。"""
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
        "context_length": row.context_length,
        "timeout": row.timeout,
        "extra": dict(row.config or {}),
    }
    if include_secrets:
        payload["api_key"] = row.api_key
    return payload


def resolve_model_spec_for_agent(
    db: Session,
    *,
    model_id: str | None,
    legacy_model: str | None = None,
    legacy_temperature: float | None = None,
    snapshot_llm: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """
    组装 Agent 时解析最终模型 spec。

    优先级：快照 llm（补全 api_key）> model_id 目录 > 遗留 model/temperature 字段。
    """
    if snapshot_llm:
        spec = dict(snapshot_llm)
        mid = spec.get("model_id")
        if mid and not spec.get("api_key"):
            live = get_model(db, str(mid))
            if live is not None:
                spec["api_key"] = live.api_key
                if not spec.get("base_url"):
                    spec["base_url"] = live.base_url
        return spec

    if model_id:
        row = get_model(db, model_id)
        if row is None:
            raise LookupError(f"Agent 绑定的模型不存在：{model_id}")
        if row.status != "active":
            raise ValueError(f"模型已禁用：{row.name} ({model_id})")
        return model_spec_from_row(row)

    if legacy_model:
        return {
            "model_name": legacy_model,
            "temperature": legacy_temperature,
        }
    return None


def ensure_default_model_from_settings(
    db: Session,
    settings: Settings | None = None,
    *,
    model_id: str = "model_default",
) -> ModelDefinition:
    """幂等：用 Settings/.env 种子一条默认模型目录项。"""
    settings = settings or get_settings()
    existing = db.get(ModelDefinition, model_id)
    if existing is not None:
        return existing
    return create_model(
        db,
        model_id=model_id,
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
    if provider not in ALLOWED_PROVIDERS:
        raise ValueError(
            f"不支持的 provider：{provider}，可选：{', '.join(sorted(ALLOWED_PROVIDERS))}"
        )
    if provider == "openai_compatible" and not base_url:
        # 允许创建时为空，运行时再回退 Settings；这里仅告警级校验放宽
        pass


def _bump_methodologies_using_model(db: Session, model_id: str) -> None:
    """模型超参数变更：bump 所有引用该模型的 Agent 所在方法论。"""
    # 延迟导入，避免与 agent_factory / revisions 循环依赖
    from deepagents_app.db.models import Methodology
    from deepagents_app.services.agent_factory import invalidate_agent_cache
    from deepagents_app.services.revisions import snapshot_methodology

    agent_ids = [
        a.id
        for a in db.query(AgentDefinition).filter(AgentDefinition.model_id == model_id).all()
    ]
    if not agent_ids:
        invalidate_agent_cache()
        return

    meth_ids = {
        link.methodology_id
        for link in db.query(MethodologyAgent)
        .filter(MethodologyAgent.agent_id.in_(agent_ids))
        .all()
    }
    for mid in meth_ids:
        methodology = db.get(Methodology, mid)
        if methodology is None:
            continue
        methodology.version += 1
        methodology.updated_time = datetime.now(timezone.utc)
        invalidate_agent_cache(methodology.id)
        db.flush()
        snapshot_methodology(db, methodology.id)
