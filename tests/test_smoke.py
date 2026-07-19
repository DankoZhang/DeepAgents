"""
不依赖 LLM 的本地冒烟测试：工具路径安全、知识库检索、子 Agent 规格完整性。

运行::

    python -m pytest tests/test_smoke.py -q
    # 或
    python tests/test_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_subagent_specs() -> None:
    from deepagents_app.subagents import build_all_subagents

    subs = build_all_subagents()
    names = {s["name"] for s in subs}
    assert names == {"document-writer", "computer-operator", "qa-expert"}
    for s in subs:
        assert s["description"]
        assert s["system_prompt"]
        assert isinstance(s["tools"], list) and s["tools"]


def test_subagent_yaml_loader_respects_enabled(tmp_path) -> None:
    """enabled: false 的条目应被跳过。"""
    from deepagents_app.subagents.loader import load_subagents_from_yaml

    cfg = tmp_path / "subagents.yaml"
    cfg.write_text(
        """
subagents:
  - name: only-qa
    description: 仅启用的问答专家
    system_prompt: 你是问答 Agent。
    tools: qa
    enabled: true
  - name: disabled-one
    description: 应被跳过
    system_prompt: unused
    tools: document
    enabled: false
""",
        encoding="utf-8",
    )
    subs = load_subagents_from_yaml(cfg)
    assert [s["name"] for s in subs] == ["only-qa"]
    assert subs[0]["tools"]


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
    # 无 pytest 时的简易运行器
    test_subagent_specs()
    test_knowledge_search()
    test_shell_whitelist()
    print("basic smoke OK (path/doc tests need pytest)")
