"""slim_text モジュールのテスト。"""

from __future__ import annotations

import pytest

from claq.lib.slim_text import compact_line


@pytest.mark.parametrize(
    ("text", "max_length", "expected"),
    [
        ("まあ  設定を   変える？", 80, "まあ 設定を 変える？"),
        ("x" * 20, 10, "xxxxxxxxxx..."),
        ("", 200, ""),
        ("   \t \n ", 200, ""),
        ("```", 200, ""),
        ("```py", 200, ""),
        ("| table |", 200, ""),
        ("# heading", 200, "heading"),
        ("- item", 200, "item"),
        ("1. numbered", 200, "numbered"),
    ],
    ids=["wording-kept", "trimmed", "empty", "whitespace-only", "fence", "fence-lang", "table",
         "heading", "bullet", "numbered"],
)
def test_compact_line(text: str, max_length: int, expected: str) -> None:
    """空白を畳みマークダウンの行頭記号を落とし、長すぎれば切って ``...`` を付ける。言い回しと句読点は変えない。フェンスと表の行は空になる。"""
    assert compact_line(text, max_length) == expected
