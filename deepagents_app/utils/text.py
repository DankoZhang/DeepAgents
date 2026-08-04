"""文本 / 消息内容归一化。"""

from __future__ import annotations

from typing import Any


def normalize_message_content(content: Any) -> str:
    """统一把 str / multimodal block 列表转为纯文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = [
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        ]
        return "\n".join(t for t in texts if t)
    return str(content)
