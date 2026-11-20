"""claq.hooks.commit_message のコミットメッセージ検証のテスト。"""

from __future__ import annotations

import pytest

from claq.hooks import commit_message

_LONG_UNCONVENTIONAL = "bad message with no conventional format and a very long subject line that keeps going."


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", None),
        ('git commit -m "feat: add new feature"', ("feat: add new feature", [])),
        ('git commit -m "feat(core): Add feature."', ("feat(core): Add feature.", ["capitalization", "punctuation"])),
        (f'git commit -m "{_LONG_UNCONVENTIONAL}"', (_LONG_UNCONVENTIONAL, ["format", "length", "punctuation"])),
        ("git commit -m \"$(cat <<'EOF'\nfix: x\nEOF\n)\"", None),
        ("git commit -m `cat msg.txt`", None),
    ],
    ids=[
        "not-a-commit",
        "lowercase-no-period",
        "capitalized-with-period",
        "unconventional-long-with-period",
        "heredoc-command-substitution",
        "backtick-command-substitution",
    ],
)
def test_validate_commit_message(command: str, expected: tuple[str, list[str]] | None) -> None:
    """commit 以外とコマンド置換の本文は None、commit はメッセージと指摘種別を返すこと。

    指摘種別は conventional 形式・72 文字超・大文字始まり・末尾ピリオド。コマンド置換は本文が実行時に
    しか決まらず、字面（`$(cat <<`）を本文として検査すると毎回の commit で形式違反の警告が出る。
    """
    result = commit_message.validate_commit_message(command)

    summary = None if result is None else (result["message"], [issue["type"] for issue in result["issues"]])
    assert summary == expected
