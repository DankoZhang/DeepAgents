"""
Skill 目录与物化
================

- 目录 CRUD：用户可维护 SKILL.md 正文（含 YAML frontmatter）
- 组装时物化到 ``workspace/skills/<scope>/<agent_id>/``，供 deepagents 加载
- 变更后默认 bump 引用该方法论；旧会话靠快照内嵌 content 重建
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.api.errors import BusinessError, NotFoundError
from deepagents_app.config import Settings
from deepagents_app.db.models import AgentSkill, SkillDefinition
from deepagents_app.ownership import validate_resource_id
from deepagents_app.services.revisions import (
    bump_methodologies_using_skill,
)
from deepagents_app.utils.paths import resolve_under_root
from deepagents_app.workspace import get_workspace_root

logger = logging.getLogger(__name__)

# name 同时用作物化目录名，故限制字符集
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def list_skills(
    db: Session,
    *,
    owner_user_id: str,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> tuple[list[SkillDefinition], int]:
    """列出当前用户的 Skill 目录；可按 status 过滤。返回 (rows, total)。"""
    from deepagents_app.api.pagination import paginate_query

    q = (
        db.query(SkillDefinition)
        .filter(SkillDefinition.owner_user_id == owner_user_id)
        .order_by(SkillDefinition.name)
    )
    if status:
        q = q.filter(SkillDefinition.status == status)
    return paginate_query(q, limit=limit, offset=offset)


def get_skill(
    db: Session, skill_id: str, *, owner_user_id: str
) -> SkillDefinition | None:
    """按主键取 Skill；不属于当前用户则视为不存在。"""
    row = db.get(SkillDefinition, skill_id)
    if row is None or row.owner_user_id != owner_user_id:
        return None
    return row


def create_skill(
    db: Session,
    *,
    owner_user_id: str,
    name: str,
    content: str,
    description: str = "",
    config: dict[str, Any] | None = None,
    status: str = "active",
    skill_id: str | None = None,
) -> SkillDefinition:
    """
    创建 Skill。

    若 ``content`` 无 YAML frontmatter，会自动用 name/description 拼一层。
    """
    name = name.strip()
    _validate_skill_name(name)
    if (
        db.query(SkillDefinition)
        .filter(
            SkillDefinition.owner_user_id == owner_user_id,
            SkillDefinition.name == name,
        )
        .one_or_none()
    ):
        raise BusinessError(f"已存在同名 Skill：{name}")

    body = (content or "").strip()
    if not body:
        raise BusinessError("Skill content 不能为空")
    # deepagents 期望 SKILL.md 带 --- frontmatter ---
    if not body.lstrip().startswith("---"):
        body = build_skill_markdown(name=name, description=description, body=body)

    row = SkillDefinition(
        id=_resolve_skill_id(skill_id),
        owner_user_id=owner_user_id,
        name=name,
        description=(description or "").strip(),
        content=body,
        config=dict(config or {}),
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def update_skill(
    db: Session,
    skill_id: str,
    *,
    owner_user_id: str,
    name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> SkillDefinition:
    """更新 Skill；``bump_related=True`` 时升版所有引用该方法论。"""
    row = get_skill(db, skill_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Skill 不存在：{skill_id}")

    if name is not None and name.strip() != row.name:
        new_name = name.strip()
        _validate_skill_name(new_name)
        clash = (
            db.query(SkillDefinition)
            .filter(
                SkillDefinition.owner_user_id == owner_user_id,
                SkillDefinition.name == new_name,
                SkillDefinition.id != skill_id,
            )
            .one_or_none()
        )
        if clash is not None:
            raise BusinessError(f"已存在同名 Skill：{new_name}")
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
    if config is not None:
        merged = dict(row.config or {})
        merged.update(config)
        row.config = merged
    if status is not None:
        row.status = status

    row.updated_time = datetime.now(timezone.utc)
    db.flush()
    if bump_related:
        bump_methodologies_using_skill(db, skill_id)
    else:
        from deepagents_app.services.revisions import (
            schedule_cache_invalidation_for_agent_ids,
        )

        agent_ids = [
            r.agent_id
            for r in db.query(AgentSkill).filter(AgentSkill.skill_id == skill_id).all()
        ]
        schedule_cache_invalidation_for_agent_ids(db, agent_ids)
    return row


def delete_skill(
    db: Session,
    skill_id: str,
    *,
    owner_user_id: str,
    bump_related: bool = True,
) -> None:
    """删除 Skill；若仍被 Agent 引用且 bump_related，则级联升版那些方法论。"""
    row = get_skill(db, skill_id, owner_user_id=owner_user_id)
    if row is None:
        raise NotFoundError(f"Skill 不存在：{skill_id}")

    # 删除前记下引用：AgentSkill 会随 Skill 行级联消失
    agent_ids = [
        r.agent_id
        for r in db.query(AgentSkill).filter(AgentSkill.skill_id == skill_id).all()
    ]
    db.delete(row)
    db.flush()
    if bump_related:
        if agent_ids:
            from deepagents_app.services.revisions import bump_methodologies_for_agent_ids

            bump_methodologies_for_agent_ids(db, agent_ids)
    elif agent_ids:
        from deepagents_app.services.revisions import (
            schedule_cache_invalidation_for_agent_ids,
        )

        schedule_cache_invalidation_for_agent_ids(db, agent_ids)


# ── 快照还原与磁盘物化（Agent Factory 组装时调用）──────────────────────


def skill_definition_from_snapshot(payload: dict[str, Any]) -> SkillDefinition:
    """从快照 dict 构造脱离 Session 的 SkillDefinition（仅供物化）。"""
    return SkillDefinition(
        id=str(payload.get("id") or payload.get("name") or ""),
        name=str(payload.get("name") or ""),
        description=str(payload.get("description") or ""),
        content=str(payload.get("content") or ""),
        config=dict(payload.get("config") or {}),
        status=str(payload.get("status") or "active"),
    )


def load_skills_from_snapshots(payloads: list[dict[str, Any]]) -> list[SkillDefinition]:
    """按快照内嵌的 Skill payload 还原（顺序保留；仅 active）。"""
    result: list[SkillDefinition] = []
    for payload in payloads:
        row = skill_definition_from_snapshot(payload)
        if (row.status or "active") != "active":
            logger.warning("快照中 Skill 已禁用，跳过：%s (%s)", row.name, row.id)
            continue
        result.append(row)
    return result


def materialize_agent_skills(
    settings: Settings,
    agent_id: str,
    skills: list[SkillDefinition],
    *,
    scope: str,
    workspace_root: Path | None = None,
) -> str | None:
    """
    把 Agent 已绑 Skills 物化到
    ``<workspace_root>/skills/<scope>/<agent_id>/<name>/SKILL.md``。

    Returns:
        供 ``skills=`` 使用的源目录虚拟路径；无可用 skill 返回 None。
    """
    active = [s for s in skills if (s.status or "active") == "active"]
    root = _safe_materialize_root(
        settings, scope=scope, agent_id=agent_id, workspace_root=workspace_root
    )
    # 每次组装前清空，避免残留旧版 SKILL.md
    if root.exists():
        shutil.rmtree(root)
    if not active:
        return None

    root.mkdir(parents=True, exist_ok=True)
    for skill in active:
        try:
            skill_dir = resolve_under_root(root, skill.name, basename_only=True)
        except ValueError as exc:
            raise BusinessError(f"Skill 物化路径非法：{skill.name}") from exc
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill.content or "", encoding="utf-8")
    return f"/skills/{scope}/{agent_id}/"


def clear_materialized_skills(
    settings: Settings,
    *,
    scope: str,
    workspace_root: Path | None = None,
) -> None:
    """清空指定 scope 下的物化 Skills（组装前调用）。"""
    dst = _safe_materialize_root(
        settings, scope=scope, workspace_root=workspace_root
    )
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)


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

    ``scope`` 形如 ``<methodology_id>/v<version>``；任一段含穿越都会被拦截。
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


def parse_skill_markdown(content: str) -> tuple[str | None, str | None]:
    """从 SKILL.md 解析 frontmatter 的 name / description（失败返回 None）。"""
    text = content.lstrip()
    if not text.startswith("---"):
        return None, None
    end = text.find("\n---", 3)
    if end < 0:
        return None, None
    fm = text[3:end].strip()
    name: str | None = None
    description: str | None = None
    lines = fm.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("name:"):
            name = line[len("name:") :].strip().strip("'\"")
            i += 1
            continue
        if line.startswith("description:"):
            rest = line[len("description:") :].strip()
            # 支持 YAML 多行块标量 > / |
            if rest in {">", "|", ""}:
                parts: list[str] = []
                i += 1
                while i < len(lines) and (
                    lines[i].startswith("  ")
                    or lines[i].startswith("\t")
                    or lines[i] == ""
                ):
                    parts.append(lines[i].strip())
                    i += 1
                description = "\n".join(p for p in parts if p).strip() or None
            else:
                description = rest.strip("'\"")
                i += 1
            continue
        i += 1
    return name, description


def import_skill_from_file(
    db: Session,
    path: Path,
    *,
    owner_user_id: str,
    skill_id: str | None = None,
    name_override: str | None = None,
) -> SkillDefinition | None:
    """从 SKILL.md 文件幂等导入（已存在同 id 或同 name 则跳过创建）。"""
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    fm_name, fm_desc = parse_skill_markdown(content)
    name = (name_override or fm_name or path.parent.name).strip()
    if skill_id:
        existing_by_id = get_skill(db, skill_id, owner_user_id=owner_user_id)
        if existing_by_id is not None:
            return existing_by_id
    existing = (
        db.query(SkillDefinition)
        .filter(
            SkillDefinition.owner_user_id == owner_user_id,
            SkillDefinition.name == name,
        )
        .one_or_none()
    )
    if existing is not None:
        return existing
    return create_skill(
        db,
        owner_user_id=owner_user_id,
        name=name,
        description=fm_desc or "",
        content=content,
        skill_id=skill_id,
    )


def _validate_skill_name(name: str) -> None:
    """校验 Skill name：既是展示名，也是物化目录名。"""
    if not _SKILL_NAME_RE.match(name):
        raise BusinessError(
            "Skill name 须为字母/数字开头，仅含字母数字、连字符、下划线（亦作物化目录名）"
        )


def _resolve_skill_id(skill_id: str | None) -> str:
    resolved = skill_id or f"skill_{uuid.uuid4().hex[:12]}"
    return validate_resource_id(resolved, label="skill id")
