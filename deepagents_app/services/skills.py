"""Skill 目录 CRUD、物化到 workspace、变更后 bump 相关方法论。"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from deepagents_app.config import Settings
from deepagents_app.db.models import (
    AgentSkill,
    SkillDefinition,
)
from deepagents_app.services.agent_factory import invalidate_agent_cache
from deepagents_app.services.revisions import bump_methodologies_using_agent

logger = logging.getLogger(__name__)

# 物化目录名：字母数字、连字符、下划线
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")


def list_skills(
    db: Session,
    *,
    status: str | None = None,
) -> list[SkillDefinition]:
    q = db.query(SkillDefinition).order_by(SkillDefinition.name)
    if status:
        q = q.filter(SkillDefinition.status == status)
    return q.all()


def get_skill(db: Session, skill_id: str) -> SkillDefinition | None:
    return db.get(SkillDefinition, skill_id)


def create_skill(
    db: Session,
    *,
    name: str,
    content: str,
    description: str = "",
    config: dict[str, Any] | None = None,
    status: str = "active",
    skill_id: str | None = None,
) -> SkillDefinition:
    name = name.strip()
    _validate_skill_name(name)
    if db.query(SkillDefinition).filter(SkillDefinition.name == name).one_or_none():
        raise ValueError(f"已存在同名 Skill：{name}")

    body = (content or "").strip()
    if not body:
        raise ValueError("Skill content 不能为空")
    # 无 frontmatter 时自动包一层，保证 SkillsMiddleware 能解析 name/description
    if not body.lstrip().startswith("---"):
        body = build_skill_markdown(name=name, description=description, body=body)

    row = SkillDefinition(
        id=skill_id or f"skill_{uuid.uuid4().hex[:12]}",
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
    name: str | None = None,
    description: str | None = None,
    content: str | None = None,
    config: dict[str, Any] | None = None,
    status: str | None = None,
    bump_related: bool = True,
) -> SkillDefinition:
    row = get_skill(db, skill_id)
    if row is None:
        raise LookupError(f"Skill 不存在：{skill_id}")

    if name is not None and name.strip() != row.name:
        new_name = name.strip()
        _validate_skill_name(new_name)
        clash = (
            db.query(SkillDefinition)
            .filter(SkillDefinition.name == new_name, SkillDefinition.id != skill_id)
            .one_or_none()
        )
        if clash is not None:
            raise ValueError(f"已存在同名 Skill：{new_name}")
        row.name = new_name

    if description is not None:
        row.description = description.strip()
    if content is not None:
        body = content.strip()
        if not body:
            raise ValueError("Skill content 不能为空")
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
        _bump_methodologies_using_skill(db, skill_id)
    else:
        invalidate_agent_cache()
    return row


def delete_skill(db: Session, skill_id: str, *, bump_related: bool = True) -> None:
    row = get_skill(db, skill_id)
    if row is None:
        raise LookupError(f"Skill 不存在：{skill_id}")

    agent_ids = [
        r.agent_id
        for r in db.query(AgentSkill).filter(AgentSkill.skill_id == skill_id).all()
    ]
    db.delete(row)
    db.flush()
    if bump_related:
        for agent_id in agent_ids:
            bump_methodologies_using_agent(db, agent_id)
    else:
        invalidate_agent_cache()


def load_skills_by_ids(db: Session, skill_ids: list[str]) -> list[SkillDefinition]:
    """按 id 列表加载（保持顺序；缺失跳过；仅返回 active）。"""
    if not skill_ids:
        return []
    rows = (
        db.query(SkillDefinition)
        .filter(SkillDefinition.id.in_(skill_ids))
        .all()
    )
    by_id = {r.id: r for r in rows}
    result: list[SkillDefinition] = []
    for sid in skill_ids:
        row = by_id.get(sid)
        if row is None:
            logger.warning("快照引用的 Skill 不存在，跳过：%s", sid)
            continue
        if row.status != "active":
            logger.warning("Skill 已禁用，跳过：%s (%s)", row.name, sid)
            continue
        result.append(row)
    return result


def materialize_agent_skills(
    settings: Settings,
    agent_id: str,
    skills: list[SkillDefinition],
    *,
    scope: str,
) -> str | None:
    """
    把 Agent 已绑 Skills 物化到 ``workspace/skills/<scope>/<agent_id>/<name>/SKILL.md``。

    ``scope`` 一般为 ``{methodology_id}/v{version}``，避免并发组装互删。

    Returns:
        供 ``skills=`` 使用的源目录虚拟路径；无可用 skill 返回 None。
    """
    active = [s for s in skills if (s.status or "active") == "active"]
    root = settings.workspace_dir / "skills" / scope / agent_id
    if root.exists():
        shutil.rmtree(root)
    if not active:
        return None

    root.mkdir(parents=True, exist_ok=True)
    for skill in active:
        skill_dir = root / skill.name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(skill.content or "", encoding="utf-8")
    return f"/skills/{scope}/{agent_id}/"


def clear_materialized_skills(settings: Settings, *, scope: str) -> None:
    """清空指定 scope 下的物化 Skills（组装前调用，避免该方法论版本残留）。"""
    dst = settings.workspace_dir / "skills" / scope
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True, exist_ok=True)


def build_skill_markdown(*, name: str, description: str, body: str) -> str:
    """用 name/description + 正文拼出带 frontmatter 的 SKILL.md。"""
    desc = (description or "").strip() or name
    # YAML 多行 description，避免特殊字符破坏 frontmatter
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
    # 简易 YAML：支持 name: xxx 与 description: > 多行
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
            if rest in {">", "|", ""}:
                parts: list[str] = []
                i += 1
                while i < len(lines) and (
                    lines[i].startswith("  ") or lines[i].startswith("\t") or lines[i] == ""
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
    skill_id: str | None = None,
    name_override: str | None = None,
) -> SkillDefinition | None:
    """从 SKILL.md 文件幂等导入（已存在同 id 或同 name 则跳过创建）。"""
    if not path.exists():
        return None
    content = path.read_text(encoding="utf-8")
    fm_name, fm_desc = parse_skill_markdown(content)
    name = (name_override or fm_name or path.parent.name).strip()
    if skill_id and db.get(SkillDefinition, skill_id) is not None:
        return db.get(SkillDefinition, skill_id)
    existing = (
        db.query(SkillDefinition).filter(SkillDefinition.name == name).one_or_none()
    )
    if existing is not None:
        return existing
    return create_skill(
        db,
        name=name,
        description=fm_desc or "",
        content=content,
        skill_id=skill_id,
    )


def _validate_skill_name(name: str) -> None:
    if not _SKILL_NAME_RE.match(name):
        raise ValueError(
            "Skill name 须为字母/数字开头，仅含字母数字、连字符、下划线（亦作物化目录名）"
        )


def _bump_methodologies_using_skill(db: Session, skill_id: str) -> None:
    links = db.query(AgentSkill).filter(AgentSkill.skill_id == skill_id).all()
    seen: set[str] = set()
    for link in links:
        if link.agent_id in seen:
            continue
        seen.add(link.agent_id)
        bump_methodologies_using_agent(db, link.agent_id)
