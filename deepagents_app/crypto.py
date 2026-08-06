"""
密钥加解密
==========

模型目录中的 ``api_key`` 落库前加密；运行时再解密。
密文前缀 ``enc:v1:``，无前缀视为历史明文（兼容迁移）。

生产必须设置 ``SECRETS_ENCRYPTION_KEY``（Fernet url-safe base64 密钥）。
未配置时派生本地开发密钥并打警告（勿用于生产）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from deepagents_app.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"


@lru_cache(maxsize=4)
def _fernet_for_key(material: str) -> Fernet:
    raw = (material or "").strip()
    if raw:
        try:
            # 已是 Fernet 密钥
            return Fernet(raw.encode("ascii") if isinstance(raw, str) else raw)
        except Exception:  # noqa: BLE001
            # 任意口令 → 稳定 32 bytes → urlsafe base64
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))
    digest = hashlib.sha256(b"deepagents-local-dev-only-not-for-prod").digest()
    logger.warning(
        "未配置 SECRETS_ENCRYPTION_KEY，使用本地开发派生密钥加密 api_key；"
        "生产环境请设置 SECRETS_ENCRYPTION_KEY"
    )
    return Fernet(base64.urlsafe_b64encode(digest))


def _get_fernet() -> Fernet:
    settings = get_settings()
    material = getattr(settings, "secrets_encryption_key", None) or ""
    return _fernet_for_key(material)


def encrypt_secret(plain: str | None) -> str | None:
    """明文 → 带前缀密文；空值原样返回。"""
    if plain is None:
        return None
    text = plain.strip()
    if not text:
        return None
    if text.startswith(_PREFIX):
        return text
    token = _get_fernet().encrypt(text.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(stored: str | None) -> str | None:
    """密文 → 明文；无前缀则当作历史明文直接返回。"""
    if stored is None:
        return None
    text = stored.strip()
    if not text:
        return None
    if not text.startswith(_PREFIX):
        return text
    token = text[len(_PREFIX) :].encode("ascii")
    try:
        return _get_fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("api_key 解密失败：密钥不匹配或密文损坏") from exc


def secret_is_present(stored: str | None) -> bool:
    """是否已配置密钥（不解密）。"""
    return bool(stored and stored.strip())


def clear_fernet_cache() -> None:
    """测试用：清空 Fernet 单例缓存。"""
    _fernet_for_key.cache_clear()
