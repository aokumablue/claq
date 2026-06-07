"""<private> 等のタグをストリップする"""

from __future__ import annotations

import re

# ストリップ対象タグ（大文字小文字区別なし）
_TAGS = (
    "private",
    "mem-context",
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

    result = text
    for pattern in _PATTERNS:
        # ReDoS保護: パターンごとに最大回数チェック
        if len(pattern.findall(result)) > _MAX_TAG_COUNT:
            continue
        result = pattern.sub("", result)

    # 連続空行を1つに
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()
