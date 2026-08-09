"""
密钥加解密
==========

模型目录中的 ``api_key`` 落库前加密；运行时再解密。
密文前缀 ``enc:v1:``；无此前缀的历史明文一律拒绝解密。

生产必须设置 ``SECRETS_ENCRYPTION_KEY``（Fernet url-safe base64 密钥）。
轮转时可把旧密钥放进 ``SECRETS_ENCRYPTION_PREVIOUS_KEYS``（逗号分隔），
解密时主密钥失败后依次尝试旧密钥；新写入只用主密钥。

未配置主密钥时：仅当 ``AUTH_DISABLED`` 或 ``SECRETS_ALLOW_INSECURE_DEV_KEY``
为真才允许固定开发派生密钥；否则 fail-fast。
"""

from __future__ import annotations

import base64
import hashlib
import logging
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from deepagents_app.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "enc:v1:"
_INSECURE_DEV_MATERIAL = b"deepagents-local-dev-only-not-for-prod"


@lru_cache(maxsize=8)
def _fernet_for_key(material: str) -> Fernet:
    raw = (material or "").strip()
    if raw:
        try:
            return Fernet(raw.encode("ascii") if isinstance(raw, str) else raw)
        except Exception:  # noqa: BLE001
            digest = hashlib.sha256(raw.encode("utf-8")).digest()
            return Fernet(base64.urlsafe_b64encode(digest))

    settings = get_settings()
    allow_insecure = bool(
        settings.auth_disabled or settings.secrets_allow_insecure_dev_key
    )
    if not allow_insecure:
        raise RuntimeError(
            "未配置 SECRETS_ENCRYPTION_KEY：生产环境必须设置该密钥。"
            "本地可用 AUTH_DISABLED=true 或 SECRETS_ALLOW_INSECURE_DEV_KEY=true，"
            "或执行："
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    digest = hashlib.sha256(_INSECURE_DEV_MATERIAL).digest()
    logger.warning(
        "未配置 SECRETS_ENCRYPTION_KEY，使用本地开发派生密钥加密 api_key；"
        "生产环境请设置 SECRETS_ENCRYPTION_KEY"
    )
    return Fernet(base64.urlsafe_b64encode(digest))


def _primary_material() -> str:
    settings = get_settings()
    return getattr(settings, "secrets_encryption_key", None) or ""


def _previous_materials() -> list[str]:
    settings = get_settings()
    raw = getattr(settings, "secrets_encryption_previous_keys", None) or ""
    return [part.strip() for part in str(raw).split(",") if part.strip()]


def _get_encrypt_fernet() -> Fernet:
    """新密文只用主密钥加密。"""
    return _fernet_for_key(_primary_material())


def _get_decrypt_fernet() -> Fernet | MultiFernet:
    """解密：主密钥 + 旧密钥链。"""
    primary = _fernet_for_key(_primary_material())
    previous = [_fernet_for_key(m) for m in _previous_materials()]
    if not previous:
        return primary
    return MultiFernet([primary, *previous])


def encrypt_secret(plain: str | None) -> str | None:
    """明文 → 带前缀密文；空值原样返回。"""
    if plain is None:
        return None
    text = plain.strip()
    if not text:
        return None
    if text.startswith(_PREFIX):
        return text
    token = _get_encrypt_fernet().encrypt(text.encode("utf-8")).decode("ascii")
    return f"{_PREFIX}{token}"


def decrypt_secret(stored: str | None) -> str | None:
    """密文 → 明文；无 ``enc:v1:`` 前缀则抛错（不再兼容明文落库）。"""
    if stored is None:
        return None
    text = stored.strip()
    if not text:
        return None
    if not text.startswith(_PREFIX):
        raise ValueError(
            "api_key 必须为 enc:v1: 密文；检测到未加密明文，请重新保存模型密钥"
        )
    token = text[len(_PREFIX) :].encode("ascii")
    try:
        return _get_decrypt_fernet().decrypt(token).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError(
            "api_key 解密失败：密钥不匹配或密文损坏"
            "（若刚轮转密钥，请配置 SECRETS_ENCRYPTION_PREVIOUS_KEYS）"
        ) from exc


def secret_is_present(stored: str | None) -> bool:
    """是否已配置密钥（不解密）。"""
    return bool(stored and stored.strip())


def clear_fernet_cache() -> None:
    """测试用：清空 Fernet 单例缓存。"""
    _fernet_for_key.cache_clear()
