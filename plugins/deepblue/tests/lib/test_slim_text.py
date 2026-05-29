"""slim_text モジュールのテスト。"""

from __future__ import annotations

from devgear.lib.slim_text import compact_line, first_meaningful_line


def test_first_meaningful_line_skips_markdown_noise() -> None:
    text = "\n".join(
        [
            "```python",
            "print('x')",
            "```",
            "# Heading",
            "- item",
            "  本文 line",
        ]
    )

    assert first_meaningful_line(text) == "Heading"


def test_compact_line_removes_filler_and_trims_length() -> None:
    assert compact_line("ご質問ありがとうございます。  えーと  設定変更することができます。", 80) == "設定変更できます"
    assert compact_line("x" * 20, 10) == "xxxxxxxxxx..."
