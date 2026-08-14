"""<private> 等のタグをストリップする"""

from __future__ import annotations

import re

# ストリップ対象タグ（大文字小文字区別なし）
_TAGS = (
    "private",
    "mem-context",
    "bluecore-memory",
    "system_instruction",
    "system-instruction",
)

# ReDoS 保護: タグ出現回数の上限
_MAX_TAG_COUNT = 100

# 事前コンパイル済みパターン
_PATTERNS = [
    re.compile(
        rf"<{tag}[^>]*>.*?</{tag}>",
        re.DOTALL | re.IGNORECASE,
    )
    for tag in _TAGS
]


def strip_tags(text: str) -> str:
    """対象タグとその中身を除去し、連続空行を詰める。"""
    if not text:
        return text

    for pattern in _PATTERNS:
        if len(pattern.findall(text)) > _MAX_TAG_COUNT:
            continue
        text = pattern.sub("", text)

    return re.sub(r"\n{3,}", "\n\n", text).strip()
