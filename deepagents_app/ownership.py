"""
用户归属辅助
============

- 资源主键在全局唯一，种子 / 默认资源用 ``scoped_id`` 派生，避免跨用户撞 id
- 名称类唯一约束在「用户内」生效（见 ORM）
"""

from __future__ import annotations

import hashlib
import re

from deepagents_app.api.errors import BusinessError
from deepagents_app.constants import DEFAULT_MODEL_ID, DEMO_METHODOLOGY_ID

# 客户端可指定主键 / thread_id / Skill name 共用：禁止路径分隔与穿越字符
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_THREAD_ID_RE = _RESOURCE_ID_RE


def validate_resource_id(resource_id: str, *, label: str = "id") -> str:
    """
    校验客户端可指定的资源主键。

    允许字母数字开头，其后仅字母数字、下划线、连字符；最长 128。
    拒绝 ``/``、``..`` 等，避免主键被拼进磁盘路径时逃逸。
    """
    value = (resource_id or "").strip()
    if not _RESOURCE_ID_RE.fullmatch(value):
        raise BusinessError(
            f"{label} 仅允许 1–128 位、字母或数字开头，"
            "其余为字母数字、下划线或连字符"
        )
    return value


def user_scope_key(owner_user_id: str) -> str:
    """把任意长度 user_id 压成稳定短后缀，便于嵌入 128 字符主键。"""
    return hashlib.sha256(owner_user_id.encode("utf-8")).hexdigest()[:12]


def scoped_id(owner_user_id: str, base_id: str) -> str:
    """逻辑 id + 用户 → 确定性全局主键。"""
    key = user_scope_key(owner_user_id)
    out = f"{base_id}__{key}"
    if len(out) <= 128:
        return out
    return hashlib.sha256(f"{base_id}:{owner_user_id}".encode("utf-8")).hexdigest()[:32]


def default_model_id_for_user(owner_user_id: str) -> str:
    return scoped_id(owner_user_id, DEFAULT_MODEL_ID)


def demo_methodology_id_for_user(owner_user_id: str) -> str:
    return scoped_id(owner_user_id, DEMO_METHODOLOGY_ID)


def checkpoint_thread_id(user_id: str, thread_id: str) -> str:
    """Checkpointer 隔离键：用户 + 业务 thread_id，防猜 thread 串读。"""
    return f"{user_id}:{thread_id}"


def validate_thread_id(thread_id: str) -> None:
    validate_resource_id(thread_id, label="thread_id")
