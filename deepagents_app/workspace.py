"""
用户工作区与跨进程锁
====================

- 每个用户独立 ``workspace/users/<scope_key>/``，避免会话间文件串读
- ``ContextVar`` 供工具在 invoke 期间解析当前根目录
- 物化 Skills 用文件锁串行化 clear/write，降低多 worker 互删概率
"""

from __future__ import annotations

import fcntl
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from deepagents_app.config import Settings, get_settings
from deepagents_app.ownership import user_scope_key

logger = logging.getLogger(__name__)

_workspace_root: ContextVar[Path | None] = ContextVar("workspace_root", default=None)


def get_workspace_root(settings: Settings | None = None) -> Path:
    """当前请求/组装上下文中的 workspace 根；未设置时回退全局 ``workspace_dir``。"""
    current = _workspace_root.get()
    if current is not None:
        return current
    cfg = settings or get_settings()
    return cfg.workspace_dir.resolve()


@contextmanager
def workspace_context(root: Path) -> Iterator[Path]:
    """在上下文内把工具 / 物化默认根切到 ``root``。"""
    resolved = root.resolve()
    token = _workspace_root.set(resolved)
    try:
        yield resolved
    finally:
        _workspace_root.reset(token)


def user_workspace_dir(
    settings: Settings, owner_user_id: str, *, ensure: bool = True
) -> Path:
    """返回该用户的工作区根目录：``workspace/users/<scope_key>/``。"""
    key = user_scope_key(owner_user_id)
    root = (settings.workspace_dir / "users" / key).resolve()
    if ensure:
        ensure_user_workspace(settings, root)
    return root


def ensure_user_workspace(settings: Settings, root: Path) -> Path:
    """创建用户工作区子目录，并同步项目级 AGENTS.md。"""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    for sub in ("documents", "notes", "audit", "skills"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    src_memory = settings.memory_file
    dst_memory = root / "AGENTS.md"
    if src_memory.exists():
        import shutil

        # 仅当缺失或源更新时覆盖，避免每次组装都打盘
        if (not dst_memory.exists()) or (
            src_memory.stat().st_mtime > dst_memory.stat().st_mtime
        ):
            shutil.copy2(src_memory, dst_memory)
    return root


@contextmanager
def interprocess_lock(lock_file: Path) -> Iterator[None]:
    """跨进程互斥（同机多 worker）；基于 ``fcntl.flock``。"""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a+", encoding="utf-8")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            logger.debug("释放文件锁失败：%s", exc)
        fh.close()


def skills_materialize_lock(workspace_root: Path, scope: str) -> Path:
    """物化目录对应的锁文件路径（scope 中的 ``/`` 换成 ``__``）。"""
    safe = scope.replace("/", "__").replace("\\", "__")
    return workspace_root / "skills" / f".lock_{safe}"
