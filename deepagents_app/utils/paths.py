"""路径安全辅助。"""

from __future__ import annotations

from pathlib import Path


def resolve_under_root(root: Path, relative_or_name: str, *, basename_only: bool = False) -> Path:
    """
    将相对路径解析到 ``root`` 内，拦截路径穿越。

    Args:
        root: 允许访问的根目录（会 resolve）
        relative_or_name: 用户传入路径；``basename_only=True`` 时只取文件名
        basename_only: 文档场景等只允许根下单层文件名

    Raises:
        ValueError: 路径越界或名为空
    """
    base = root.resolve()
    if basename_only:
        name = Path(relative_or_name).name
        if not name:
            raise ValueError("文件名不能为空")
        target = (base / name).resolve()
    else:
        candidate = Path(relative_or_name)
        target = (
            (base / candidate).resolve()
            if not candidate.is_absolute()
            else candidate.resolve()
        )
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"路径越界，仅允许访问：{base}") from exc
    return target
