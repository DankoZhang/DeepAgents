"""
不依赖 LLM 的本地冒烟测试：知识库检索。

运行::

    python -m pytest tests/test_smoke.py -q
"""

from __future__ import annotations


def test_knowledge_search() -> None:
    from deepagents_app.tools.qa_tools import search_knowledge

    result = search_knowledge.invoke({"query": "middleware 中间件"})
    assert "kb-003" in result or "Middleware" in result


def test_list_knowledge_topics() -> None:
    from deepagents_app.tools.qa_tools import list_knowledge_topics

    result = list_knowledge_topics.invoke({})
    assert "Middleware" in result or "kb-" in result


if __name__ == "__main__":
    test_knowledge_search()
    test_list_knowledge_topics()
    print("basic smoke OK")
