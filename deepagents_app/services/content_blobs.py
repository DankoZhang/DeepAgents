"""
内容寻址正文（Content Blob）
============================

Skill / system_prompt / Memory 等大段正文按 sha256 去重存储。
方法论快照只存 ``content_hash``，旧会话按 hash 取回不可变正文。

GC：删除未被任何 ``MethodologyRevision.snapshot`` 引用的孤儿 blob
（写路径不调用；由后台调度 / CLI 执行）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.db.models import ContentBlob, MethodologyRevision

logger = logging.getLogger(__name__)


def content_hash(text: str) -> str:
    """正文 → sha256 hex。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


async def ensure_content_blob(db: AsyncSession, text: str) -> str:
    """
    将正文写入 content_blob（若尚则插入），返回 hash。

    空串也入库，保证引用稳定。并发同 hash 插入时吞掉主键冲突。
    """
    body = text if text is not None else ""
    digest = content_hash(body)
    existing = await db.get(ContentBlob, digest)
    if existing is not None:
        return digest
    try:
        async with db.begin_nested():
            db.add(
                ContentBlob(
                    hash=digest,
                    content=body,
                    created_time=datetime.now(timezone.utc),
                )
            )
            await db.flush()
    except IntegrityError:
        # 并发同 hash：另一方已插入
        logger.debug("content_blob 并发插入冲突，复用已有 hash=%s", digest)
    return digest


async def get_content_blob(db: AsyncSession, digest: str) -> str | None:
    """按 hash 取正文；不存在返回 None。"""
    if not digest:
        return None
    row = await db.get(ContentBlob, digest)
    return None if row is None else row.content


async def get_content_blobs(
    db: AsyncSession, digests: set[str]
) -> dict[str, str]:
    """批量按 hash 取正文；返回 hash → content。"""
    wanted = {d for d in digests if d}
    if not wanted:
        return {}
    rows = await db.scalars(
        select(ContentBlob).where(ContentBlob.hash.in_(wanted))
    )
    return {row.hash: row.content for row in rows}


def _collect_hashes_from_snapshot(snapshot: dict[str, Any] | None) -> set[str]:
    """从一份方法论快照 JSON 收集所有 content_hash。"""
    found: set[str] = set()
    if not isinstance(snapshot, dict):
        return found

    mem = snapshot.get("memory")
    if isinstance(mem, dict):
        h = mem.get("content_hash")
        if isinstance(h, str) and h:
            found.add(h)

    for agent in snapshot.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        h = agent.get("system_prompt_hash")
        if isinstance(h, str) and h:
            found.add(h)
        for skill in agent.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            sh = skill.get("content_hash")
            if isinstance(sh, str) and sh:
                found.add(sh)
    return found


async def collect_referenced_content_hashes(db: AsyncSession) -> set[str]:
    """扫描全部 revision 快照，返回仍被引用的 hash 集合。"""
    referenced: set[str] = set()
    # 只取 snapshot 列，避免加载整行无关字段
    rows = await db.scalars(select(MethodologyRevision.snapshot))
    for snapshot in rows:
        referenced |= _collect_hashes_from_snapshot(snapshot)
    return referenced


async def hydrate_snapshot_content(
    db: AsyncSession, snapshot: dict[str, Any] | None
) -> dict[str, Any]:
    """
    将快照中的 ``*_hash`` 解析为正文，供组装使用。

    兼容旧快照：若已有 ``content`` / ``system_prompt`` 明文则保留。
    一次 IN 查询批量取回所需 blob，避免 N+1。
    """
    if not isinstance(snapshot, dict):
        return {}
    out = dict(snapshot)

    needed: set[str] = set()
    mem = out.get("memory")
    if isinstance(mem, dict) and "content" not in mem and mem.get("content_hash"):
        needed.add(str(mem["content_hash"]))
    for agent in out.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        if not agent.get("system_prompt") and agent.get("system_prompt_hash"):
            needed.add(str(agent["system_prompt_hash"]))
        for skill in agent.get("skills") or []:
            if isinstance(skill, dict) and not skill.get("content") and skill.get(
                "content_hash"
            ):
                needed.add(str(skill["content_hash"]))

    bodies = await get_content_blobs(db, needed)

    if isinstance(mem, dict):
        mem = dict(mem)
        if "content" not in mem and mem.get("content_hash"):
            mem["content"] = bodies.get(str(mem["content_hash"]), "")
        out["memory"] = mem

    agents: list[dict[str, Any]] = []
    for agent in out.get("agents") or []:
        if not isinstance(agent, dict):
            continue
        a = dict(agent)
        if not a.get("system_prompt") and a.get("system_prompt_hash"):
            a["system_prompt"] = bodies.get(str(a["system_prompt_hash"]), "")
        skills: list[dict[str, Any]] = []
        for skill in a.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            s = dict(skill)
            if not s.get("content") and s.get("content_hash"):
                s["content"] = bodies.get(str(s["content_hash"]), "")
            skills.append(s)
        a["skills"] = skills
        agents.append(a)
    out["agents"] = agents
    return out


async def gc_orphan_content_blobs(db: AsyncSession) -> int:
    """
    删除未被任何 revision 快照引用的 content_blob。

    仅应由后台调度 / CLI 调用，不要挂在草稿保存等写路径上。

    先快照候选 hash，再收集引用，避免「新写入的 blob 落在两次查询缝隙」被误删。

    Returns:
        删除行数。
    """
    # 1) 先固定候选集合
    hashes = list(await db.scalars(select(ContentBlob.hash)))
    if not hashes:
        return 0
    # 2) 再扫引用（此间新建的 blob 不在 hashes 中，不会被删）
    referenced = await collect_referenced_content_hashes(db)
    orphans = [h for h in hashes if h not in referenced]
    if not orphans:
        return 0
    result = await db.execute(
        delete(ContentBlob).where(ContentBlob.hash.in_(orphans))
    )
    deleted = int(result.rowcount or 0)
    await db.flush()
    if deleted:
        logger.info("已清理孤儿 content_blob：%s", deleted)
    return deleted
