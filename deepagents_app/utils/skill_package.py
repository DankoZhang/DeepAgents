#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
@File    :   skill_package.py
@Time    :   2026/08/16 18:46:00
@Author  :   zhangce
@Desc    :   skill_package.py

Skill 目录包解析
================

把 zip / 技能目录规范成 ``SKILL.md`` + 附属文件映射。
上传与种子导入共用；不落盘、不长期保存 zip。
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from deepagents_app.api.errors import BusinessError
from deepagents_app.config import Settings, get_settings

_SKILL_MD = "SKILL.md"
_DEFAULT_SUFFIXES = (".md", ".py", ".sh", ".json", ".yaml", ".yml", ".txt", ".toml", ".csv")
_SKIP_PARTS = frozenset({"__macosx"})
_SKIP_NAMES = frozenset({".ds_store", "thumbs.db"})
_UNIX_SYMLINK = 0xA000
_MAX_RATIO = 100.0
_RATIO_MIN_BYTES = 64 * 1024


@dataclass(frozen=True)
class SkillPackage:
    """已校验的技能包：``content`` 为 SKILL.md，``files`` 为附属文本。"""

    name: str
    description: str
    content: str
    files: dict[str, str]


def skill_files_map(files: Mapping[str, Any] | None) -> dict[str, str]:
    """
    将库内 / 快照中的 files 规范为 ``{相对路径: 正文}``。

    空值视为无附属文件；路径非法则拒绝（物化前再拦一层）。
    """
    if not files:
        return {}
    if not isinstance(files, Mapping):
        raise BusinessError("Skill files 必须是对象（相对路径 → 正文）")
    out: dict[str, str] = {}
    for raw_path, raw_body in files.items():
        path = normalize_skill_relpath(str(raw_path))
        if path == _SKILL_MD:
            raise BusinessError("附属文件不能覆盖 SKILL.md")
        if not isinstance(raw_body, str):
            raise BusinessError(f"附属文件须为文本：{path}")
        out[path] = raw_body
    return dict(sorted(out.items()))


def normalize_skill_relpath(raw: str, *, max_depth: int | None = None) -> str:
    """规范化技能包内相对路径：正斜杠、禁止穿越 / 隐藏段。"""
    text = (raw or "").replace("\\", "/").strip()
    if not text or text.startswith("/"):
        raise BusinessError(f"非法技能文件路径：{raw!r}")
    if ":" in text.split("/", 1)[0]:
        raise BusinessError(f"非法技能文件路径：{raw!r}")
    parts: list[str] = []
    for part in text.split("/"):
        if part in {"", "."}:
            continue
        if part == ".." or part.startswith("."):
            raise BusinessError(f"非法技能文件路径：{raw!r}")
        parts.append(part)
    if not parts:
        raise BusinessError(f"非法技能文件路径：{raw!r}")
    depth = max_depth if max_depth is not None else 4
    if len(parts) > depth:
        raise BusinessError(f"技能文件路径过深：{raw}")
    return "/".join(parts)


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


def load_skill_package_from_bytes(
    data: bytes,
    *,
    filename: str | None = None,
    settings: Settings | None = None,
) -> SkillPackage:
    """从 zip 字节解析技能包。"""
    cfg = settings or get_settings()
    name = (filename or "").strip()
    if name and not name.lower().endswith(".zip"):
        raise BusinessError("技能包须为 .zip")
    max_bytes = int(cfg.skill_package_max_bytes)
    if len(data) > max_bytes:
        raise BusinessError(f"技能包超过大小上限（{max_bytes} 字节）")
    if not data:
        raise BusinessError("技能包不能为空")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise BusinessError("不是有效的 zip 技能包") from exc
    with zf:
        mapping = _read_zip_mapping(zf, cfg)
    return package_from_file_map(mapping, settings=cfg)


def load_skill_package_from_dir(
    path: Path,
    *,
    settings: Settings | None = None,
) -> SkillPackage:
    """
    从技能目录或其中的 ``SKILL.md`` 解析。

    只收集该技能目录内文件，不向上遍历。
    """
    cfg = settings or get_settings()
    root = path
    if path.is_file():
        if path.name != _SKILL_MD:
            raise BusinessError("须指向 SKILL.md 或技能目录")
        root = path.parent
    if not root.is_dir():
        raise BusinessError(f"技能目录不存在：{path}")
    mapping: dict[str, bytes] = {}
    for file_path in sorted(root.rglob("*")):
        if file_path.is_symlink():
            raise BusinessError(f"技能目录含符号链接：{file_path.name}")
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root).as_posix()
        if _skip_entry(rel):
            continue
        mapping[rel] = file_path.read_bytes()
        if len(mapping) > int(cfg.skill_package_max_files):
            raise BusinessError(
                f"技能包文件数超过上限（{cfg.skill_package_max_files}）"
            )
    return package_from_file_map(mapping, settings=cfg, fallback_name=root.name)


def package_from_file_map(
    mapping: Mapping[str, bytes],
    *,
    settings: Settings | None = None,
    fallback_name: str | None = None,
) -> SkillPackage:
    """将「相对路径 → 字节」规范成 SkillPackage。"""
    cfg = settings or get_settings()
    if not mapping:
        raise BusinessError("技能包没有文件")
    prefix = _skill_root_prefix(list(mapping))
    stripped: dict[str, bytes] = {}
    for raw_name, blob in mapping.items():
        rel = raw_name[len(prefix) :] if prefix and raw_name.startswith(prefix) else raw_name
        if not rel or rel.endswith("/"):
            continue
        if _skip_entry(rel):
            continue
        path = normalize_skill_relpath(
            rel, max_depth=int(cfg.skill_package_max_depth)
        )
        stripped[path] = blob

    if _SKILL_MD not in stripped:
        raise BusinessError("技能包必须在根目录包含 SKILL.md")

    suffixes = _allowed_suffixes(cfg)
    files: dict[str, str] = {}
    content = _decode_text(stripped[_SKILL_MD], _SKILL_MD)
    for path, blob in stripped.items():
        if path == _SKILL_MD:
            continue
        _assert_allowed_suffix(path, suffixes)
        files[path] = _decode_text(blob, path)

    fm_name, fm_desc = parse_skill_markdown(content)
    name = (fm_name or fallback_name or "").strip()
    if not name:
        raise BusinessError("无法从 SKILL.md 解析 name，且未提供目录名")
    return SkillPackage(
        name=name,
        description=(fm_desc or "").strip(),
        content=content,
        files=dict(sorted(files.items())),
    )


def _skill_root_prefix(names: list[str]) -> str:
    """
    zip 根有 SKILL.md → 无前缀；
    否则须为单一顶层目录，且该目录根下有 SKILL.md。
    """
    normalized: list[str] = []
    for raw in names:
        item = raw.replace("\\", "/").strip("/")
        if not item or _skip_entry(item):
            continue
        normalized.append(item)
    if _SKILL_MD in normalized:
        return ""
    tops = {item.split("/", 1)[0] for item in normalized}
    if len(tops) == 1:
        top = next(iter(tops))
        inner = f"{top}/{_SKILL_MD}"
        if inner in normalized:
            return f"{top}/"
    raise BusinessError("技能包须在根目录或单一顶层目录下包含 SKILL.md")


def _read_zip_mapping(zf: zipfile.ZipFile, settings: Settings) -> dict[str, bytes]:
    max_files = int(settings.skill_package_max_files)
    max_uncompressed = int(settings.skill_package_max_uncompressed_bytes)
    mapping: dict[str, bytes] = {}
    total = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        if _is_zip_symlink(info):
            raise BusinessError("技能包不能包含符号链接")
        name = info.filename.replace("\\", "/")
        if _skip_entry(name):
            continue
        if name.startswith("/") or any(part == ".." for part in name.split("/")):
            raise BusinessError(f"非法技能文件路径：{name!r}")
        if len(mapping) >= max_files:
            raise BusinessError(f"技能包文件数超过上限（{max_files}）")
        declared = int(info.file_size or 0)
        if declared > max_uncompressed:
            raise BusinessError("技能包未压缩体积超过上限")
        payload = zf.read(info)
        if len(payload) > max_uncompressed:
            raise BusinessError("技能包未压缩体积超过上限")
        total += len(payload)
        if total > max_uncompressed:
            raise BusinessError("技能包未压缩体积超过上限")
        compressed = int(info.compress_size or 0)
        if (
            compressed > 0
            and len(payload) >= _RATIO_MIN_BYTES
            and (len(payload) / compressed) > _MAX_RATIO
        ):
            raise BusinessError("技能包压缩比异常")
        mapping[name] = payload
    return mapping


def _is_zip_symlink(info: zipfile.ZipInfo) -> bool:
    return (info.external_attr >> 16) & 0xF000 == _UNIX_SYMLINK


def _skip_entry(name: str) -> bool:
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    if any(p.lower() in _SKIP_PARTS for p in parts):
        return True
    if parts and parts[-1].lower() in _SKIP_NAMES:
        return True
    return False


def _allowed_suffixes(settings: Settings) -> set[str]:
    raw = (settings.skill_package_allowed_suffixes or "").strip()
    if not raw:
        return {s.lower() for s in _DEFAULT_SUFFIXES}
    return {
        (part.strip().lower() if part.strip().startswith(".") else f".{part.strip().lower()}")
        for part in raw.split(",")
        if part.strip()
    }


def _assert_allowed_suffix(path: str, suffixes: set[str]) -> None:
    suffix = Path(path).suffix.lower()
    if suffix not in suffixes:
        raise BusinessError(f"不支持的技能文件类型：{path}")


def _decode_text(blob: bytes, path: str) -> str:
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BusinessError(f"技能文件不是 UTF-8 文本：{path}") from exc
