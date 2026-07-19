"""
模型工厂
========

根据配置创建 Chat Model 实例。

支持：
- openai
- anthropic
- openai_compatible（任意兼容 OpenAI API 的端点）
"""

from __future__ import annotations

import logging

from langchain_core.language_models.chat_models import BaseChatModel

from deepagents_app.config import Settings

logger = logging.getLogger(__name__)


def build_chat_model(
    settings: Settings,
    *,
    model_name: str | None = None,
    temperature: float | None = None,
) -> BaseChatModel:
    """按 ``settings.model_provider`` 实例化聊天模型。"""
    provider = settings.model_provider
    name = model_name or settings.model_name
    temp = 0.2 if temperature is None else temperature

    if provider == "openai":
        from langchain_openai import ChatOpenAI

        kwargs: dict = {"model": name, "temperature": temp}
        if settings.openai_api_key:
            kwargs["api_key"] = settings.openai_api_key
        if settings.openai_base_url:
            kwargs["base_url"] = settings.openai_base_url
        logger.info("使用 OpenAI 模型：%s (temp=%s)", name, temp)
        return ChatOpenAI(**kwargs)

    if provider == "openai_compatible":
        from langchain_openai import ChatOpenAI

        if not settings.openai_base_url:
            raise ValueError("openai_compatible 模式必须设置 OPENAI_BASE_URL")
        logger.info(
            "使用兼容 OpenAI API 的模型：%s @ %s (temp=%s)",
            name,
            settings.openai_base_url,
            temp,
        )
        return ChatOpenAI(
            model=name,
            api_key=settings.openai_api_key or "EMPTY",
            base_url=settings.openai_base_url,
            temperature=temp,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        kwargs = {"model": name, "temperature": temp}
        if settings.anthropic_api_key:
            kwargs["api_key"] = settings.anthropic_api_key
        logger.info("使用 Anthropic 模型：%s (temp=%s)", name, temp)
        return ChatAnthropic(**kwargs)

    raise ValueError(f"不支持的 model_provider：{provider}")
