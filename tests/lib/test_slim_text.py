"""slim_text モジュールのテスト。"""

from __future__ import annotations

import pytest

from claq.lib import slim_text


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
        ("  -   item  ", 200, "item"),
        ("#   heading", 200, "heading"),
        ("2)  x", 200, "x"),
    ],
    ids=["wording-kept", "trimmed", "empty", "whitespace-only", "fence", "fence-lang", "table",
         "heading", "bullet", "numbered", "bullet-padded", "heading-padded", "numbered-padded"],
)
def test_compact_line(text: str, max_length: int, expected: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """空白を畳みマークダウンの行頭記号を落とし、長すぎれば切って ``...`` を付ける。言い回しと句読点は変えない。フェンスと表の行は空になる。"""
    monkeypatch.setattr(slim_text, "COMPACT_LINE_MAX_CHARS", max_length)
    assert slim_text.compact_line(text) == expected
