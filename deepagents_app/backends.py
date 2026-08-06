"""
Backend 组装
============

deepagents 的虚拟文件系统由 Backend 提供。本模块返回绑定到
**用户工作区根** 的 FilesystemBackend（默认 ``workspace/users/<key>/``）。
"""

from __future__ import annotations

from pathlib import Path

from deepagents.backends import FilesystemBackend

from deepagents_app.config import Settings
from deepagents_app.workspace import get_workspace_root


def build_filesystem_backend(
    settings: Settings,
    *,
    workspace_root: Path | None = None,
) -> FilesystemBackend:
    """创建以用户（或显式传入）workspace 为根的本地文件系统 backend。"""
    root = (workspace_root or get_workspace_root(settings)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    # virtual_mode=True：强制虚拟路径语义，阻止 ``..`` / 绝对路径逃逸 root_dir
    return FilesystemBackend(
        root_dir=str(root),
        virtual_mode=True,
    )
