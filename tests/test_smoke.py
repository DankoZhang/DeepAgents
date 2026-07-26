"""
不依赖 LLM 的本地冒烟测试：工具路径安全、知识库检索。

运行::

    python -m pytest tests/test_smoke.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_knowledge_search() -> None:
    from deepagents_app.tools.qa_tools import search_knowledge

    result = search_knowledge.invoke({"query": "middleware 中间件"})
    assert "kb-003" in result or "Middleware" in result


def test_path_traversal_blocked() -> None:
    from deepagents_app.tools.computer_tools import _safe_path
    import pytest

    with pytest.raises(ValueError):
        _safe_path("../../etc/passwd")


def test_shell_whitelist() -> None:
    from deepagents_app.tools.computer_tools import _validate_command

    assert _validate_command("ls -la") is None
    assert _validate_command("rm -rf /") is not None
    assert _validate_command("sudo reboot") is not None


def test_document_roundtrip(tmp_path, monkeypatch) -> None:
    from deepagents_app import config
    from deepagents_app.config import Settings

    ws = tmp_path / "workspace"
    (ws / "documents").mkdir(parents=True)
    settings = Settings(workspace_dir=ws)
    config.get_settings.cache_clear()
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from deepagents_app.tools.document_tools import create_document, read_document, list_documents

    msg = create_document.invoke(
        {"filename": "demo.md", "title": "Demo", "content": "hello deepagents"}
    )
    assert "demo.md" in msg
    listed = list_documents.invoke({})
    assert "demo.md" in listed
    body = read_document.invoke({"filename": "demo.md"})
    assert "hello deepagents" in body


if __name__ == "__main__":
    test_knowledge_search()
    test_shell_whitelist()
    print("basic smoke OK (path/doc tests need pytest)")
