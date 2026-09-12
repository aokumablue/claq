"""slim_text モジュールのテスト。"""

from __future__ import annotations

import pytest

from claq.lib.slim_text import compact_line, first_meaningful_line, remove_filler_phrases


class TestRemoveFillerPhrases:
    """埋め草削除は「行の途中から文字を消す唯一の処理」である。

    この性質に ``mem/tag_stripping`` の再鍛造検査が乗っている。ここに新しい
    削除を足すと、escape 済みの本文からタグを組み上げ直す経路が復活する
    （``<system-まあreminder>`` → ``<system-reminder>``）。
    """

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("まあ そうする", " そうする"),
            ("<system-まあreminder>", "<system-reminder>"),
            ("えーとちなみに一応とりあえず基本的にざっくり言うと", ""),
            ("削るものが無い", "削るものが無い"),
        ],
        ids=["leading", "mid-token", "every-phrase", "no-op"],
    )
    def test_phrases_are_removed_regardless_of_position(self, text: str, expected: str) -> None:
        """埋め草は位置に関わらず消える。"""
        assert remove_filler_phrases(text) == expected

    def test_no_other_transformation_is_applied(self) -> None:
        """空白の畳み込みや語句の言い換えはしない（削除だけを担う）。"""
        assert remove_filler_phrases("  a   b  ") == "  a   b  "


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


def test_strip_markdown_prefix_variants() -> None:
    """各種マークダウン記号を正しく処理する。"""
    from claq.lib.slim_text import _strip_markdown_prefix

    assert _strip_markdown_prefix("") == ""
    assert _strip_markdown_prefix("```py") == ""
    assert _strip_markdown_prefix("| table |") == ""
    assert _strip_markdown_prefix("# heading") == "heading"
    assert _strip_markdown_prefix("- item") == "item"
    assert _strip_markdown_prefix("1. numbered") == "numbered"


def test_first_meaningful_line_all_stripped() -> None:
    """記号のみで意味のある行が無ければ空文字を返す。"""
    from claq.lib.slim_text import first_meaningful_line

    assert first_meaningful_line("```") == ""


def test_normalize_line_markdown_only_becomes_empty() -> None:
    """マークダウン記号のみの行は正規化で空になる。"""
    from claq.lib.slim_text import _normalize_line

    assert _normalize_line("```") == ""


def test_first_meaningful_line_skips_blank_and_table_only_lines() -> None:
    """空行とテーブル記号のみの行を飛ばして次の実質行を返す。"""
    text = "\n".join(["", "| a | b |", "実質行"])

    assert first_meaningful_line(text) == "実質行"


def test_normalize_line_whitespace_only_becomes_empty() -> None:
    """空白のみの行は分割段階で空になる。"""
    from claq.lib.slim_text import _normalize_line

    assert _normalize_line("   \t \n ") == ""


def test_compact_line_returns_empty_for_unnormalizable_text() -> None:
    """正規化で空になるテキストは compact_line も空を返す。"""
    assert compact_line("```", 200) == ""
