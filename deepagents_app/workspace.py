#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   workspace.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   workspace.py

用户工作区
==========

本模块负责「用户级文件沙箱」的路径解析与请求内绑定：

- 每个用户独立 ``workspace/users/<scope_key>/``，避免会话间文件串读
- ``_workspace_root``（ContextVar）供工具在 invoke 期间解析当前根目录，
  无需层层传参；未进入 ``workspace_context`` 时回退全局 ``workspace_dir``

典型调用链（chat / agent 组装）::

    root = user_workspace_dir(settings, user_id)   # 算路径并确保目录存在
    with workspace_context(root):                 # 绑定 ContextVar
        ...  # get_workspace_root() / backend / 工具读写都落在 root
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from deepagents_app.config import Settings, get_settings
from deepagents_app.ownership import user_scope_key

# 请求/任务级「当前 workspace 根」。async 与同步线程都能正确隔离，
# 比全局变量安全；比给每个工具加 workspace_root 参数更省事。
_workspace_root: ContextVar[Path | None] = ContextVar("workspace_root", default=None)


def get_workspace_root(settings: Settings | None = None) -> Path:
    """
    解析当前应使用的 workspace 根目录。

    优先读 ContextVar（``workspace_context`` 已绑定用户目录）；
    若未绑定（例如启动脚本、未进 chat 路径），回退到 Settings 的全局
    ``workspace_dir``。
    """
    current = _workspace_root.get()
    if current is not None:
        return current
    cfg = settings or get_settings()
    return cfg.workspace_dir.resolve()


@contextmanager
def workspace_context(root: Path) -> Iterator[Path]:
    """
    在 ``with`` 作用域内把当前 workspace 根切到 ``root``。

    进入时 ``set``，退出时 ``reset``（含异常路径），保证不污染后续请求。
    chat invoke / agent 组装 / Skills 物化前应包一层本上下文。
    """
    resolved = root.resolve()
    token = _workspace_root.set(resolved)
    try:
        yield resolved
    finally:
        _workspace_root.reset(token)


def user_workspace_dir(
    settings: Settings, owner_user_id: str, *, ensure: bool = True
) -> Path:
    """
    计算某用户的工作区根：``workspace/users/<scope_key>/``。

    ``scope_key`` 由 ``user_scope_key`` 从 user_id 哈希得到，路径短且稳定。
    ``ensure=True``（默认）时会创建子目录；
    仅查询路径时可 ``ensure=False`` 避免多余 IO。
    """
    key = user_scope_key(owner_user_id)
    root = (settings.workspace_dir / "users" / key).resolve()
    if ensure:
        ensure_user_workspace(root)
    return root


def ensure_user_workspace(root: Path) -> Path:
    """
    幂等初始化用户工作区布局。

    子目录：documents / notes / audit / skills。
    Agent 注入的 Memory 以方法论快照版本化物化为准（见 ``services.versioning.memory``）。
    """
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("documents", "notes", "audit", "skills"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root
