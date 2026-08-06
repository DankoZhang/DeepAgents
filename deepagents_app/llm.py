"""
Chat Model 工厂
===============

根据配置创建 Chat Model 实例。

支持：
- openai
- anthropic
- openai_compatible（任意兼容 OpenAI API 的端点）
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from langchain_core.language_models.chat_models import BaseChatModel

from deepagents_app.api.errors import BusinessError
from deepagents_app.config import Settings

logger = logging.getLogger(__name__)


def build_chat_model(
    settings: Settings,
    *,
    model_name: str | None = None,
    temperature: float | None = None,
    top_p: float | None = None,
    max_tokens: int | None = None,
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> BaseChatModel:
    """
    按 provider 实例化聊天模型。

    参数优先使用显式传入值，缺省回退 ``settings`` / 约定默认值。
    ``extra`` 会合并进构造参数（勿覆盖已显式设置的键）。
    """
    resolved_provider = provider or settings.model_provider
    name = model_name or settings.model_name
    temp = 0.2 if temperature is None else temperature

    kwargs: dict[str, Any] = {"model": name, "temperature": temp}
    if top_p is not None:
        kwargs["top_p"] = top_p
    if max_tokens is not None:
        kwargs["max_tokens"] = max_tokens
    if timeout is not None:
        kwargs["timeout"] = timeout
    if extra:
        for key, value in extra.items():
            kwargs.setdefault(key, value)

    if resolved_provider == "openai":
        from langchain_openai import ChatOpenAI

        key = api_key if api_key is not None else settings.openai_api_key
        url = base_url if base_url is not None else settings.openai_base_url
        if key:
            kwargs["api_key"] = key
        if url:
            kwargs["base_url"] = url
        logger.info(
            "使用 OpenAI 模型：%s (temp=%s top_p=%s max_tokens=%s)",
            name,
            temp,
            top_p,
            max_tokens,
        )
        return ChatOpenAI(**kwargs)

    if resolved_provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        url = base_url if base_url is not None else settings.openai_base_url
        if not url:
            raise BusinessError("openai_compatible 模式必须设置 base_url / OPENAI_BASE_URL")
        key = api_key if api_key is not None else settings.openai_api_key
        # 写入 kwargs（覆盖 extra 同名键），避免 ChatOpenAI(api_key=..., **kwargs) 重复传参
        kwargs["api_key"] = key or "EMPTY"
        kwargs["base_url"] = url
        logger.info(
            "使用兼容 OpenAI API 的模型：%s @ %s (temp=%s top_p=%s max_tokens=%s)",
            name,
            url,
            temp,
            top_p,
            max_tokens,
        )
        return ChatOpenAI(**kwargs)

    if resolved_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        key = api_key if api_key is not None else settings.anthropic_api_key
        if key:
            kwargs["api_key"] = key
        logger.info(
            "使用 Anthropic 模型：%s (temp=%s top_p=%s max_tokens=%s)",
            name,
            temp,
            top_p,
            max_tokens,
        )
        return ChatAnthropic(**kwargs)

    raise BusinessError(f"不支持的 model_provider：{resolved_provider}")


def model_spec_from_row(row: Any) -> dict[str, Any]:
    """ORM ModelDefinition → build_chat_model 可用的参数字典（含解密后密钥）。"""
    from deepagents_app.crypto import decrypt_secret

    return {
        "provider": row.provider,
        "model_name": row.model_name,
        "api_key": decrypt_secret(row.api_key),
        "base_url": row.base_url,
        "temperature": row.temperature,
        "top_p": row.top_p,
        "max_tokens": row.max_tokens,
        "timeout": row.timeout,
        "extra": dict(row.config or {}),
        "context_length": row.context_length,
        "model_id": row.id,
        "display_name": row.name,
    }


def build_chat_model_from_spec(
    settings: Settings,
    spec: Mapping[str, Any] | None,
) -> BaseChatModel:
    """从快照 / 目录序列化的 spec 构建模型；spec 为空则用 Settings。"""
    spec = dict(spec or {})
    return build_chat_model(
        settings,
        provider=spec.get("provider"),
        model_name=spec.get("model_name"),
        api_key=spec.get("api_key"),
        base_url=spec.get("base_url"),
        temperature=spec.get("temperature"),
        top_p=spec.get("top_p"),
        max_tokens=spec.get("max_tokens"),
        timeout=spec.get("timeout"),
        extra=spec.get("extra") if isinstance(spec.get("extra"), Mapping) else None,
    )
