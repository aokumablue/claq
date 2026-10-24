"""slim_text モジュールのテスト。"""

from __future__ import annotations

import pytest

from claq.lib.slim_text import compact_line, remove_filler_phrases


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("まあ そうする", " そうする"),
        ("<system-まあreminder>", "<system-reminder>"),
        ("えーとちなみに一応とりあえず基本的にざっくり言うと", ""),
        ("削るものが無い", "削るものが無い"),
        ("  a   b  ", "  a   b  "),
    ],
    ids=["leading", "mid-token", "every-phrase", "no-op", "whitespace-kept"],
)
def test_remove_filler_phrases(text: str, expected: str) -> None:
    """埋め草は位置に関わらず消え、空白の畳み込みや言い換えはしない（削除だけを担う）。

    「行の途中から文字を消す唯一の処理」であり、``mem/tag_stripping`` の再鍛造検査がこの性質に乗っている。
    ここに新しい削除を足すと、escape 済みの本文からタグを組み上げ直す経路が復活する。
    """
    assert remove_filler_phrases(text) == expected


@pytest.mark.parametrize(
    ("text", "max_length", "expected"),
    [
        ("ご質問ありがとうございます。  えーと  設定変更することができます。", 80, "設定変更できます"),
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
    ids=["phrases-and-replacement", "trimmed", "empty", "whitespace-only", "fence", "fence-lang", "table",
         "heading", "bullet", "numbered"],
)
def test_compact_line(text: str, max_length: int, expected: str) -> None:
    """定型句・埋め草・冗長表現とマークダウン記号を落とし、長すぎれば切って ``...`` を付ける。フェンスと表の行は空になる。"""
    assert compact_line(text, max_length) == expected
