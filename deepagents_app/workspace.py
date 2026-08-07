"""
用户工作区与跨进程锁
====================

本模块负责「用户级文件沙箱」的路径解析与请求内绑定：

- 每个用户独立 ``workspace/users/<scope_key>/``，避免会话间文件串读
- ``_workspace_root``（ContextVar）供工具在 invoke 期间解析当前根目录，
  无需层层传参；未进入 ``workspace_context`` 时回退全局 ``workspace_dir``
- 物化 Skills 用 ``interprocess_lock``（Unix fcntl / Windows msvcrt）串行化 clear/write

典型调用链（chat / agent 组装）::

    root = user_workspace_dir(settings, user_id)   # 算路径并确保目录存在
    with workspace_context(root):                 # 绑定 ContextVar
        ...  # get_workspace_root() / backend / 工具读写都落在 root
"""

from __future__ import annotations

import logging
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from deepagents_app.config import Settings, get_settings
from deepagents_app.ownership import user_scope_key

logger = logging.getLogger(__name__)

try:
    import fcntl as _fcntl
except ImportError:  # Windows 无 fcntl
    _fcntl = None  # type: ignore[assignment]

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
    ``ensure=True``（默认）时会创建子目录并同步 AGENTS.md；
    仅查询路径时可 ``ensure=False`` 避免多余 IO。
    """
    key = user_scope_key(owner_user_id)
    root = (settings.workspace_dir / "users" / key).resolve()
    if ensure:
        ensure_user_workspace(settings, root)
    return root


def ensure_user_workspace(settings: Settings, root: Path) -> Path:
    """
    幂等初始化用户工作区布局，并同步项目级 Memory 文件。

    - 子目录：documents / notes / audit / skills
    - 若 Settings.memory_file（项目 AGENTS.md）存在，则复制到用户根下；
      仅在目标缺失或源文件更新时覆盖，避免每次组装都打盘。
    """
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
    """
    同机多进程互斥锁。

    - Unix：``fcntl.flock`` 排他锁
    - Windows：``msvcrt.locking`` 字节锁

    用于 Skills 物化等「先清后写」临界区，防止多个 worker 互相删目录。
    注意：依赖本机文件锁，跨机器无效。
    """
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_file, "a+", encoding="utf-8")
    try:
        _acquire_file_lock(fh)
        try:
            yield
        finally:
            _release_file_lock(fh)
    finally:
        fh.close()


def _ensure_lock_byte(fh) -> None:
    """保证锁文件至少 1 字节，供 Windows ``msvcrt.locking`` 锁定。"""
    fh.seek(0, 2)
    if fh.tell() < 1:
        fh.write("0")
        fh.flush()


def _acquire_file_lock(fh) -> None:
    if _fcntl is not None:
        _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX)
        return
    if sys.platform == "win32":
        import msvcrt

        _ensure_lock_byte(fh)
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
        return
    raise RuntimeError("当前平台不支持进程文件锁（需要 fcntl 或 msvcrt）")


def _release_file_lock(fh) -> None:
    try:
        if _fcntl is not None:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
            return
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError as exc:
        logger.debug("释放文件锁失败：%s", exc)


def skills_materialize_lock(workspace_root: Path, scope: str) -> Path:
    """
    返回某 Skills 物化 scope 对应的锁文件路径。

    ``scope`` 里的路径分隔符换成 ``__``，避免锁文件名带目录层级。
    例：scope ``demo/v1`` → ``<workspace>/skills/.lock_demo__v1``。
    """
    safe = scope.replace("/", "__").replace("\\", "__")
    return workspace_root / "skills" / f".lock_{safe}"
