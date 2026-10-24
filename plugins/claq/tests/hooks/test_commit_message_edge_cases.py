"""claq.hooks.commit_message のコミットメッセージ検証のテスト。"""

from __future__ import annotations

import pytest

from claq.hooks import commit_message


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git status", None),
        ('git commit -m "feat: add new feature"', ("feat: add new feature", [])),
        ('git commit -m "feat(core): Add feature."', ("feat(core): Add feature.", ["capitalization", "punctuation"])),
    ],
    ids=["not-a-commit", "lowercase-no-period", "capitalized-with-period"],
)
def test_validate_commit_message(command: str, expected: tuple[str, list[str]] | None) -> None:
    """commit 以外は None、commit はメッセージと指摘種別（大文字始まり・末尾ピリオド）を返すこと。"""
    result = commit_message.validate_commit_message(command)

    summary = None if result is None else (result["message"], [issue["type"] for issue in result["issues"]])
    assert summary == expected
