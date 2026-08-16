#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   skills.py
@Time    :   2026/08/16 18:09:54
@Author  :   zhangce
@Desc    :   skills.py

Skill 目录与物化
================

- 目录 CRUD：JSON 创建只写 SKILL.md；上传 zip 额外写入 files 附属文件
- 组装时按内容哈希物化到 ``workspace/skills/<fingerprint>/<agent_id>/``
  （只写不删；已发布目录可跨缓存生命周期复用）
- 变更后默认 bump 引用该方法论；旧会话靠快照 content_hash + content_blob 还原重建
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import Settings
from deepagents_app.db.models import AgentSkill, SkillDefinition
from deepagents_app.db.pagination import DEFAULT_LIMIT, page_rows
from deepagents_app.services.catalog.crud_helpers import (
    ensure_unique_owned_name,
    get_owned,
    resolve_resource_id,
)
from deepagents_app.services.versioning.revisions import (
    propagate_methodology_change_for_agent_ids,
    propagate_methodology_change_using_resource,
)
from deepagents_app.utils.paths import resolve_under_root
from deepagents_app.utils.skill_package import (
    SkillPackage,
    load_skill_package_from_dir,
    skill_files_map,
)
from deepagents_app.workspace import get_workspace_root

logger = logging.getLogger(__name__)

# name 同时用作物化目录名，故限制字符集
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")
_COMPLETE_MARKER = ".complete"


async def list_skills(
    db: AsyncSession,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> tuple[list[SkillDefinition], int, str | None]:
    """列出当前用户的 Skill 目录；可按 status 过滤。返回 (rows, total, next_cursor)。"""

    stmt = (
        select(SkillDefinition)
        .where(SkillDefinition.owner_user_id == owner_user_id)
        .order_by(SkillDefinition.name, SkillDefinition.id)
    )
    if status:
        stmt = stmt.where(SkillDefinition.status == status)
    return await page_rows(
        db,
        stmt,
        limit=limit,
        cursor=cursor,
        sort_column=SkillDefinition.name,
        id_column=SkillDefinition.id,
        sort_attr="name",
    )


async def get_skill(
    db: AsyncSession, skill_id: str, *, owner_user_id: str
) -> SkillDefinition | None:
    """按主键取 Skill；不属于当前用户则视为不存在。"""

    return await get_owned(db, SkillDefinition, skill_id, owner_user_id=owner_user_id)


async def create_skill(
    db: AsyncSession,
    *,
    owner_user_id: str,
    name: str,
    content: str,
    description: str = "",
    config: dict[str, Any] | None = None,
    status: str = "active",
    skill_id: str | None = None,
    files: dict[str, str] | None = None,
) -> SkillDefinition:
    """
    创建 Skill。

    若 ``content`` 无 YAML frontmatter，会自动用 name/description 拼一层。
    ``files`` 为附属文本（相对路径 → 正文）；JSON 创建接口不传则视为空。
    """
    name = name.strip()
    _validate_skill_name(name)

    await ensure_unique_owned_name(
        db,
        SkillDefinition,
        owner_user_id=owner_user_id,
        name=name,
        label="Skill",
    )

    body = (content or "").strip()
    if not body:
        raise BusinessError("Skill content 不能为空")
    # deepagents 期望 SKILL.md 带 --- frontmatter ---
    if not body.lstrip().startswith("---"):
        body = build_skill_markdown(name=name, description=description, body=body)

    row = SkillDefinition(
        id=resolve_resource_id(skill_id, prefix="skill_", label="skill id"),
        owner_user_id=owner_user_id,
        name=name,
        description=(description or "").strip(),
        content=body,
        files=skill_files_map(files),
        config=dict(config or {}),
        status=status,
    )
    db.add(row)
    await db.flush()
    return row


async def update_skill(
    db: AsyncSession,
    skill_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    files: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> SkillDefinition:
    """更新 Skill；``files is None`` 时保留已有附属文件（PATCH content 不丢包）。"""
    row = await get_skill(db, skill_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Skill 不存在：{skill_id}")

    if name is not None and name.strip() != row.name:
        new_name = name.strip()
        _validate_skill_name(new_name)
        await ensure_unique_owned_name(
            db,
            SkillDefinition,
            owner_user_id=owner_user_id,
            name=new_name,
            exclude_id=skill_id,
            label="Skill",
        )
        row.name = new_name

    if description is not None:
        row.description = description.strip()
    if content is not None:
        body = content.strip()
        if not body:
            raise BusinessError("Skill content 不能为空")
        if not body.lstrip().startswith("---"):
            body = build_skill_markdown(
                name=row.name,
                description=row.description,
                body=body,
            )
        row.content = body
    if files is not None:
        row.files = skill_files_map(files)
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if status is not None:
        row.status = status

    row.updated_time = datetime.now(timezone.utc)
    await db.flush()
    await propagate_methodology_change_using_resource(
        db,
        kind="skill",
        resource_id=skill_id,
        bump_related=bump_related,
    )
    return row


async def delete_skill(
    db: AsyncSession,
    skill_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """删除 Skill；若仍被 Agent 引用且 bump_related，则级联升版那些方法论。"""
    row = await get_skill(db, skill_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Skill 不存在：{skill_id}")

    # 删除前记下引用：AgentSkill 会随 Skill 行级联消失
    agent_ids = [
        r.agent_id
        for r in await db.scalars(
            select(AgentSkill).where(AgentSkill.skill_id == skill_id)
        )
    ]
    await db.delete(row)
    await db.flush()
    await propagate_methodology_change_for_agent_ids(
        db, agent_ids, bump_related=bump_related
    )


async def create_skill_from_package(
    db: AsyncSession,
    package: SkillPackage,
    *,
    owner_user_id: str,
    name_override: str | None = None,
    description_override: str | None = None,
    status: str = "active",
    skill_id: str | None = None,
) -> SkillDefinition:
    """由已解析的目录包创建 Skill（上传接口）。"""
    name, description = _package_identity(
        package, name_override=name_override, description_override=description_override
    )
    return await create_skill(
        db,
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        content=package.content,
        files=package.files,
        status=status,
        skill_id=skill_id,
    )


async def replace_skill_from_package(
    db: AsyncSession,
    skill_id: str,
    package: SkillPackage,
    *,
    owner_user_id: str,
    name_override: str | None = None,
    description_override: str | None = None,
    status: str | None = None,
) -> SkillDefinition:
    """用目录包整包替换已有 Skill 的 SKILL.md 与附属文件。"""
    name, description = _package_identity(
        package, name_override=name_override, description_override=description_override
    )
    return await update_skill(
        db,
        skill_id,
        owner_user_id=owner_user_id,
        name=name,
        description=description,
        content=package.content,
        files=package.files,
        status=status,
    )


def _package_identity(
    package: SkillPackage,
    *,
    name_override: str | None,
    description_override: str | None,
) -> tuple[str, str]:
    name = (name_override or package.name).strip()
    description = (
        package.description if description_override is None else description_override
    )
    return name, (description or "").strip()


# ── 运行时 payload 还原与磁盘物化（Agent Factory 组装时调用）──────────


def skill_definition_from_payload(payload: dict[str, Any]) -> SkillDefinition:
    """从内嵌 payload 构造脱离 Session 的 SkillDefinition（仅供物化）。

    ``files`` 须为 hydrate 之后的 ``{相对路径: 正文}``。
    """
    return SkillDefinition(
        id=str(payload.get("id") or payload.get("name") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        content=str(payload.get("content") or ""),
        files=skill_files_map(payload.get("files")),
        config=dict(payload.get("config") or {}),
        status=str(payload.get("status") or "active"),
    )


def load_skills_from_payloads(payloads: list[dict[str, Any]]) -> list[SkillDefinition]:
    """按内嵌 Skill payload 还原（live / 快照同形；顺序保留；仅 active）。"""
    result: list[SkillDefinition] = []
    for payload in payloads:
        row = skill_definition_from_payload(payload)
        if (row.status or "active") != "active":
            logger.warning("Skill 已禁用，跳过：%s (%s)", row.name, row.id)
            continue
        result.append(row)
    return result


def skills_fingerprint(skills: Sequence[SkillDefinition]) -> str:
    """
    同一组 active Skill 内容 → 稳定目录名（sha256 前 16 位）。

    路径与内容一一对应，物化目录可只写不删。
    """
    active = [s for s in skills if (s.status or "active") == "active"]
    payload = [
        {
            "name": s.name,
            "content": s.content or "",
            "files": skill_files_map(s.files),
            "status": s.status or "active",
        }
        for s in sorted(active, key=lambda row: row.name)
    ]
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def materialize_agent_skills(
    settings: Settings,
    agent_id: str,
    skills: list[SkillDefinition],
    *,
    workspace_root: Path | None = None,
) -> str | None:
    """
    把 Agent 已绑 Skills 物化到
    ``<workspace_root>/skills/<fingerprint>/<agent_id>/<name>/``
    （SKILL.md + 附属文件）。

    内容寻址 + 原子发布：已有 ``.complete`` 则复用；否则写临时目录再 rename。
    缓存淘汰 / 版本裁剪不再删除这些目录。

    Returns:
        供 ``skills=`` 使用的源目录虚拟路径；无可用 skill 返回 None。
    """
    # 空 skills 会提前返回，所以这里必须先校验；有 skills 时
    # ``_safe_materialize_root`` 还会再校验一次（防御性重复，可接受）。
    _assert_safe_path_segment(agent_id, label="agent id")
    active = [s for s in skills if (s.status or "active") == "active"]
    if not active:
        return None

    scope = skills_fingerprint(active)
    root = _safe_materialize_root(
        settings, scope=scope, agent_id=agent_id, workspace_root=workspace_root
    )
    virtual = f"/skills/{scope}/{agent_id}/"

    if (root / _COMPLETE_MARKER).exists():
        # 刷新完成标记 mtime，供 GC 判断「近期仍在用」
        try:
            (root / _COMPLETE_MARKER).touch()
        except OSError as exc:
            logger.debug("刷新 Skills .complete mtime 失败：%s", exc)
        return virtual

    # 崩溃留下的半成品目录：清掉后再发布（完整目录不会走这里）
    if root.exists():
        shutil.rmtree(root)

    tmp = root.with_name(f"{root.name}.tmp-{uuid.uuid4().hex[:8]}")
    try:
        _write_skills_tree(tmp, active)
        try:
            os.rename(tmp, root)
        except OSError:
            # 目标已是目录（他进程抢先发布，或残留半成品）
            shutil.rmtree(tmp, ignore_errors=True)
            if (root / _COMPLETE_MARKER).exists():
                return virtual
            if root.exists():
                shutil.rmtree(root, ignore_errors=True)
            # 清掉半成品后再试一次
            tmp = root.with_name(f"{root.name}.tmp-{uuid.uuid4().hex[:8]}")
            _write_skills_tree(tmp, active)
            try:
                os.rename(tmp, root)
            except OSError:
                shutil.rmtree(tmp, ignore_errors=True)
                if (root / _COMPLETE_MARKER).exists():
                    return virtual
                raise BusinessError(
                    f"Skills 物化发布冲突，请重试：{scope}/{agent_id}"
                )
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise

    return virtual


def materialized_skills_dir_from_virtual(
    workspace_root: Path, virtual_path: str
) -> Path | None:
    """
    将 ``/skills/<scope>/<agent_id>/`` 映射为工作区内的物化目录。

    非法或越界路径返回 None（调用方跳过 touch，不影响聊天）。
    """
    text = (virtual_path or "").strip().strip("/")
    parts = [p for p in text.split("/") if p]
    if len(parts) < 3 or parts[0] != "skills":
        return None
    scope, agent_id = parts[1], parts[2]
    try:
        _assert_safe_path_segment(scope, label="skills scope")
        _assert_safe_path_segment(agent_id, label="agent id")
        base = (workspace_root / "skills").resolve()
        return resolve_under_root(base, f"{scope}/{agent_id}")
    except (BusinessError, ValueError, OSError) as exc:
        logger.debug("解析 Skills 物化路径失败 virtual=%r: %s", virtual_path, exc)
        return None


def touch_materialized_skills_complete(roots: Sequence[Path]) -> int:
    """
    刷新物化目录上的 ``.complete`` mtime，供 GC 判断「近期仍在用」。

    Agent LRU 命中时调用，避免长期缓存命中导致目录被误删。
    """
    touched = 0
    for root in roots:
        marker = Path(root) / _COMPLETE_MARKER
        if not marker.is_file():
            continue
        try:
            marker.touch()
            touched += 1
        except OSError as exc:
            logger.debug("刷新 Skills .complete mtime 失败：%s", exc)
    return touched


def _write_skills_tree(root: Path, skills: Sequence[SkillDefinition]) -> None:
    """写入临时目录：各 Skill 子目录（SKILL.md + 附属文件）+ 完成标记。"""
    root.mkdir(parents=True, exist_ok=False)
    for skill in skills:
        try:
            skill_dir = resolve_under_root(root, skill.name, basename_only=True)
        except ValueError as exc:
            raise BusinessError(f"Skill 物化路径非法：{skill.name}") from exc
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill.content or "", encoding="utf-8")
        for rel, body in skill_files_map(skill.files).items():
            try:
                target = resolve_under_root(skill_dir, rel)
            except ValueError as exc:
                raise BusinessError(f"Skill 附属文件路径非法：{rel}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
    (root / _COMPLETE_MARKER).touch()


def _skills_workspace_root(
    settings: Settings, *, workspace_root: Path | None = None
) -> Path:
    base = (workspace_root or get_workspace_root(settings)).resolve()
    root = (base / "skills").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _assert_safe_path_segment(segment: str, *, label: str) -> None:
    """拒绝 ``.`` / ``..`` / 含分隔符的路径段（防御历史脏主键）。"""
    value = segment or ""
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or value != Path(value).name
    ):
        raise BusinessError(f"非法 {label} 路径段：{segment!r}")


def _safe_materialize_root(
    settings: Settings,
    *,
    scope: str,
    agent_id: str | None = None,
    workspace_root: Path | None = None,
) -> Path:
    """
    解析物化目录，强制落在 ``<workspace_root>/skills`` 内。

    ``scope`` 为内容指纹（单段）；任一段含穿越都会被拦截。
    """
    scope_parts = Path(scope).parts
    if not scope_parts:
        raise BusinessError("Skills scope 不能为空")
    for part in scope_parts:
        _assert_safe_path_segment(part, label="skills scope")
    if agent_id is not None:
        _assert_safe_path_segment(agent_id, label="agent id")

    relative = f"{scope}/{agent_id}" if agent_id else scope
    try:
        return resolve_under_root(
            _skills_workspace_root(settings, workspace_root=workspace_root),
            relative,
        )
    except ValueError as exc:
        raise BusinessError(f"Skills 物化路径越界：{relative}") from exc


def gc_materialized_skills(
    settings: Settings,
    *,
    max_age_days: float | None = None,
    tmp_max_age_hours: float | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """
    清理过期的内容寻址 Skills 目录。

    策略（与组装解耦，不碰「仍在用」的路径）：
    - 复用物化时会 touch ``.complete``；超过 ``max_age_days`` 未刷新的 agent 目录删除
    - 清理残留 ``*.tmp-*`` 临时目录（超过 ``tmp_max_age_hours``）
    - 删空后的 fingerprint 父目录一并去掉

    Returns:
        ``{"removed_agents": n, "removed_tmp": n, "removed_empty_scopes": n}``
    """
    import time

    age_days = (
        float(settings.skills_gc_max_age_days)
        if max_age_days is None
        else float(max_age_days)
    )
    tmp_hours = (
        float(settings.skills_gc_tmp_max_age_hours)
        if tmp_max_age_hours is None
        else float(tmp_max_age_hours)
    )
    if age_days <= 0:
        return {
            "removed_agents": 0,
            "removed_tmp": 0,
            "removed_empty_scopes": 0,
        }

    cutoff_complete = (now if now is not None else time.time()) - age_days * 86400
    cutoff_tmp = (now if now is not None else time.time()) - tmp_hours * 3600
    users_root = (settings.workspace_dir / "users").resolve()
    stats = {"removed_agents": 0, "removed_tmp": 0, "removed_empty_scopes": 0}
    if not users_root.is_dir():
        return stats

    for user_dir in users_root.iterdir():
        if not user_dir.is_dir():
            continue
        skills_root = user_dir / "skills"
        if not skills_root.is_dir():
            continue
        try:
            skills_root.resolve().relative_to(user_dir.resolve())
        except ValueError:
            continue

        for scope_dir in list(skills_root.iterdir()):
            if not scope_dir.is_dir():
                continue
            # fingerprint 级临时目录（少见，防御性清理）
            if ".tmp-" in scope_dir.name:
                mtime = _mtime_or_none(scope_dir)
                if mtime is not None and mtime < cutoff_tmp:
                    shutil.rmtree(scope_dir, ignore_errors=True)
                    stats["removed_tmp"] += 1
                continue

            for child in list(scope_dir.iterdir()):
                if not child.is_dir():
                    continue
                if ".tmp-" in child.name:
                    mtime = _mtime_or_none(child)
                    if mtime is not None and mtime < cutoff_tmp:
                        shutil.rmtree(child, ignore_errors=True)
                        stats["removed_tmp"] += 1
                    continue

                marker = child / _COMPLETE_MARKER
                if marker.exists():
                    mtime = _mtime_or_none(marker)
                else:
                    mtime = _mtime_or_none(child)
                if mtime is not None and mtime < cutoff_complete:
                    shutil.rmtree(child, ignore_errors=True)
                    stats["removed_agents"] += 1

            try:
                if scope_dir.is_dir() and not any(scope_dir.iterdir()):
                    scope_dir.rmdir()
                    stats["removed_empty_scopes"] += 1
            except OSError:
                pass

    logger.info(
        "Skills GC 完成 removed_agents=%s removed_tmp=%s removed_empty_scopes=%s "
        "max_age_days=%s",
        stats["removed_agents"],
        stats["removed_tmp"],
        stats["removed_empty_scopes"],
        age_days,
    )
    return stats


def _mtime_or_none(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def build_skill_markdown(*, name: str, description: str, body: str) -> str:
    """用 name/description + 正文拼出带 frontmatter 的 SKILL.md。"""
    desc = (description or "").strip() or name
    desc_yaml = desc.replace("\r\n", "\n")
    if "\n" in desc_yaml:
        desc_block = ">\n  " + "\n  ".join(desc_yaml.split("\n"))
    else:
        desc_block = desc_yaml
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: {desc_block}\n"
        f"---\n\n"
        f"{body.lstrip()}"
    )


async def import_skill_from_path(
    db: AsyncSession,
    path: Path,
    *,
    owner_user_id: str,
    skill_id: str | None = None,
    name_override: str | None = None,
) -> SkillDefinition | None:
    """从 SKILL.md 或技能目录幂等导入（已存在同 id 或同 name 则跳过创建）。"""
    if not path.exists():
        return None
    package = load_skill_package_from_dir(path)
    name = (name_override or package.name).strip()
    if skill_id:
        existing_by_id = await get_skill(db, skill_id, owner_user_id=owner_user_id)
        if existing_by_id is not None:
            return existing_by_id
    existing = (
        await db.scalars(
            select(SkillDefinition).where(
                SkillDefinition.owner_user_id == owner_user_id,
                SkillDefinition.name == name,
            )
        )
    ).one_or_none()
    if existing is not None:
        return existing
    return await create_skill_from_package(
        db,
        package,
        owner_user_id=owner_user_id,
        name_override=name_override,
        skill_id=skill_id,
    )


def _validate_skill_name(name: str) -> None:
    """校验 Skill name：既是展示名，也是物化目录名。"""
    if not _SKILL_NAME_RE.match(name):
        raise BusinessError(
            "Skill name 须为字母/数字开头，仅含字母数字、连字符、下划线（亦作物化目录名）"
        )
