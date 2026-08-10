"""
Memory（AGENTS.md）版本化
========================

项目级 ``Settings.memory_file`` 在快照时钉进方法论 revision；
组装时按 ``(methodology_id, version)`` 物化到用户工作区，旧会话不随全局 Memory 漂移。
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.config import Settings, get_settings
from deepagents_app.services.versioning.content_blobs import ensure_content_blob

logger = logging.getLogger(__name__)

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def read_project_memory(settings: Settings | None = None) -> str:
    """读取项目级 AGENTS.md；不存在则返回空串。"""
    settings = settings or get_settings()
    path = settings.memory_file
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("读取 Memory 失败 %s: %s", path, exc)
        return ""


def _safe_segment(value: str, *, label: str) -> str:
    text = (value or "").strip()
    if _SEGMENT_RE.fullmatch(text):
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
    logger.warning("Memory 路径段非法（%s=%r），改用哈希 %s", label, value, digest)
    return digest


def materialize_versioned_memory(
    workspace_root: Path,
    *,
    methodology_id: str,
    version: int,
    content: str | None,
) -> str | None:
    """
    将 Memory 正文物化到用户工作区版本路径，返回 create_deep_agent 可用的虚拟路径。

    落盘：``memory/<methodology_id>/v<version>/AGENTS.md``
    虚拟：``/memory/<methodology_id>/v<version>/AGENTS.md``
    """
    if content is None:
        return None
    text = content
    # 允许空正文：不注入 memory
    if not text.strip():
        return None

    meth = _safe_segment(methodology_id, label="methodology_id")
    ver = f"v{int(version)}"
    rel = Path("memory") / meth / ver / "AGENTS.md"
    dest = (workspace_root / rel).resolve()
    try:
        dest.relative_to(workspace_root.resolve())
    except ValueError as exc:
        raise ValueError(f"Memory 物化路径越界：{rel}") from exc

    dest.parent.mkdir(parents=True, exist_ok=True)
    if (not dest.exists()) or dest.read_text(encoding="utf-8") != text:
        dest.write_text(text, encoding="utf-8")
        logger.debug("已物化 Memory → %s", dest)
    return "/" + rel.as_posix()


async def memory_payload_for_snapshot_async(
    db: AsyncSession, settings: Settings | None = None
) -> dict[str, str]:
    """写入方法论快照的 Memory 片段（正文进 content_blob）。"""
    content = read_project_memory(settings)
    digest = await ensure_content_blob(db, content)
    return {"content_hash": digest}
