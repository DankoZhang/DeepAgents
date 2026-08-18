#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   test_skill_package.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   test_skill_package.py

Skill 目录包：解包校验、物化子树、上传 API。
"""

from __future__ import annotations

import io
import zipfile

import pytest


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return buf.getvalue()


def _skill_md(name: str = "pkg-skill", body: str = "# hello\n") -> str:
    return f"---\nname: {name}\ndescription: demo package\n---\n\n{body}"


def test_package_from_wrapped_dir_and_rejects_traversal():
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.utils.skill_package import load_skill_package_from_bytes

    wrapped = _zip_bytes(
        {
            "pkg-skill/SKILL.md": _skill_md(),
            "pkg-skill/scripts/run.py": "print(1)\n",
            "pkg-skill/references/api.md": "# api\n",
        }
    )
    package = load_skill_package_from_bytes(wrapped, filename="skill.zip")
    assert package.name == "pkg-skill"
    assert package.files["scripts/run.py"] == "print(1)\n"
    assert "SKILL.md" not in package.files

    with pytest.raises(BusinessError, match="非法技能文件路径"):
        load_skill_package_from_bytes(
            _zip_bytes({"../SKILL.md": _skill_md()}),
            filename="bad.zip",
        )


def test_package_rejects_non_zip_name_and_missing_skill_md():
    from deepagents_app.api.errors import BusinessError
    from deepagents_app.utils.skill_package import load_skill_package_from_bytes

    data = _zip_bytes({"pkg-skill/SKILL.md": _skill_md()})
    with pytest.raises(BusinessError, match=".zip"):
        load_skill_package_from_bytes(data, filename="skill.tar")
    with pytest.raises(BusinessError, match="SKILL.md"):
        load_skill_package_from_bytes(
            _zip_bytes({"pkg-skill/scripts/run.py": "print(1)\n"}),
            filename="skill.zip",
        )


def test_fingerprint_changes_when_files_change():
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.services.catalog.skills import skills_fingerprint

    content = _skill_md("fp-skill")
    a = SkillDefinition(
        id="a", name="fp-skill", content=content, files={}, status="active"
    )
    b = SkillDefinition(
        id="b",
        name="fp-skill",
        content=content,
        files={"scripts/run.py": "print(1)\n"},
        status="active",
    )
    assert skills_fingerprint([a]) != skills_fingerprint([b])


def test_materialize_writes_nested_files(tmp_path, monkeypatch):
    from deepagents_app import config
    from deepagents_app.config import Settings
    from deepagents_app.db.models import SkillDefinition
    from deepagents_app.services.catalog.skills import (
        materialize_agent_skills,
        skills_fingerprint,
    )

    ws = tmp_path / "workspace"
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    skill = SkillDefinition(
        id="sk1",
        name="pkg-skill",
        description="d",
        content=_skill_md(),
        files={"scripts/run.py": "print(1)\n"},
        status="active",
    )
    virtual = materialize_agent_skills(
        settings, "agent1", [skill], workspace_root=ws
    )
    fp = skills_fingerprint([skill])
    root = ws / "skills" / fp / "agent1" / "pkg-skill"
    assert virtual == f"/skills/{fp}/agent1/"
    assert (root / "SKILL.md").is_file()
    assert (root / "scripts" / "run.py").read_text(encoding="utf-8") == "print(1)\n"


def test_build_permissions_denies_skills_write():
    from deepagents_app.factory import build_permissions

    deny_skills = False
    for perm in build_permissions():
        paths = list(getattr(perm, "paths", None) or [])
        if "/skills/**" in paths and getattr(perm, "mode", None) == "deny":
            deny_skills = True
    assert deny_skills


def test_skill_upload_patch_keeps_files(client):
    raw = _zip_bytes(
        {
            "pkg-skill/SKILL.md": _skill_md(),
            "pkg-skill/scripts/run.py": "print(1)\n",
        }
    )
    created = client.post(
        "/api/skill/upload",
        files={"file": ("skill.zip", raw, "application/zip")},
    )
    assert created.status_code == 200, created.text
    skill = created.json()
    assert skill["name"] == "pkg-skill"
    assert skill["files"]["scripts/run.py"] == "print(1)\n"

    patched = client.patch(
        f"/api/skill/{skill['id']}",
        json={"content": _skill_md(body="# updated\n")},
    )
    assert patched.status_code == 200
    assert "# updated" in patched.json()["content"]
    assert patched.json()["files"]["scripts/run.py"] == "print(1)\n"

    replaced = client.post(
        f"/api/skill/{skill['id']}/upload",
        files={
            "file": (
                "skill.zip",
                _zip_bytes(
                    {
                        "pkg-skill/SKILL.md": _skill_md(body="# v2\n"),
                        "pkg-skill/references/api.md": "# api\n",
                    }
                ),
                "application/zip",
            )
        },
    )
    assert replaced.status_code == 200, replaced.text
    body = replaced.json()
    assert "# v2" in body["content"]
    assert body["files"] == {"references/api.md": "# api\n"}


def test_skill_upload_snapshot_locks_files(client):
    import asyncio

    from deepagents_app.db.session import get_async_session_factory
    from deepagents_app.services.versioning.content_blobs import hydrate_snapshot_content
    from deepagents_app.services.versioning.revisions import get_revision

    skill = client.post(
        "/api/skill/upload",
        files={
            "file": (
                "skill.zip",
                _zip_bytes(
                    {
                        "lock-pkg/SKILL.md": _skill_md("lock-pkg", "skill body v1\n"),
                        "lock-pkg/scripts/run.py": "print('v1')\n",
                    }
                ),
                "application/zip",
            )
        },
    ).json()
    client.post("/api/methodology", json={"name": "pkg锁定", "id": "pkg_lock"})
    agent = client.post(
        "/api/agent",
        json={
            "name": "pkg-supervisor",
            "system_prompt": "supervisor",
            "config": {"role": "supervisor", "enabled": True},
            "skill_ids": [skill["id"]],
        },
    ).json()
    client.post(
        "/api/methodology/pkg_lock/agents",
        json={"agent_ids": [agent["id"]], "replace": True},
    )
    published = client.post("/api/methodology/pkg_lock/publish")
    assert published.status_code == 200
    v1 = published.json()["version"]

    client.post(
        f"/api/skill/{skill['id']}/upload",
        files={
            "file": (
                "skill.zip",
                _zip_bytes(
                    {
                        "lock-pkg/SKILL.md": _skill_md("lock-pkg", "skill body v2\n"),
                        "lock-pkg/scripts/run.py": "print('v2')\n",
                    }
                ),
                "application/zip",
            )
        },
    )

    async def _hydrate_v1():
        async with get_async_session_factory()() as db:
            rev = await get_revision(db, "pkg_lock", v1)
            snap_skill = rev.snapshot["agents"][0]["skills"][0]
            assert "content" not in snap_skill
            assert snap_skill["files"][0]["path"] == "scripts/run.py"
            assert "content_hash" in snap_skill["files"][0]
            hydrated = await hydrate_snapshot_content(db, rev.snapshot)
            return hydrated["agents"][0]["skills"][0]

    restored = asyncio.run(_hydrate_v1())
    assert "skill body v1" in restored["content"]
    assert restored["files"]["scripts/run.py"] == "print('v1')\n"
    assert "v2" not in restored["content"]
    assert "v2" not in restored["files"]["scripts/run.py"]
