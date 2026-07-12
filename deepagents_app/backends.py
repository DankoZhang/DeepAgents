"""
Backend 组装
============

deepagents 的虚拟文件系统由 Backend 提供。常见选择：

- ``StateBackend``：文件存在 LangGraph state 里（默认，无持久化到磁盘）
- ``FilesystemBackend``：映射到真实本地目录（本演示采用）
- ``StoreBackend``：落到 LangGraph Store（跨 thread 长期记忆）
- ``CompositeBackend``：按路径前缀路由到不同 backend

本模块返回绑定到 ``workspace_dir`` 的 FilesystemBackend，
让 Agent 的 ``ls`` / ``read_file`` / ``write_file`` 等工具直接操作本地工作区。
"""

from __future__ import annotations

from deepagents.backends import FilesystemBackend

from deepagents_app.config import Settings


def build_filesystem_backend(settings: Settings) -> FilesystemBackend:
    """创建以 workspace 为根的本地文件系统 backend。"""
    settings.workspace_dir.mkdir(parents=True, exist_ok=True)
    # virtual_mode=True：强制虚拟路径语义，阻止 ``..`` / 绝对路径逃逸 root_dir
    return FilesystemBackend(
        root_dir=str(settings.workspace_dir),
        virtual_mode=True,
    )
