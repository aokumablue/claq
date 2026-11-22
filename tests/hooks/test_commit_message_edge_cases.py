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
        (f'git commit -m "fix: short subject\n\n{_LONG_UNCONVENTIONAL}"', (f"fix: short subject\n\n{_LONG_UNCONVENTIONAL}", [])),
        ('git commit -am "Fix: Bad."', ("Fix: Bad.", ["format", "punctuation"])),
        ('git commit -qm "feat: quiet"', ("feat: quiet", [])),
        ('git commit -m"feat: attached"', ("feat: attached", [])),
        ('git commit --message="docs: long form"', ("docs: long form", [])),
        ('git commit --message "docs: separate value"', ("docs: separate value", [])),
        ('git commit -m "fix: don\'t crash"', ("fix: don't crash", [])),
        ('git commit -m "feat!: drop python 3.11" -m "body."', ("feat!: drop python 3.11", [])),
        ('git commit -m "feat(api)!: remove v1"', ("feat(api)!: remove v1", [])),
        ("git commit -Cmain", None),
        ("git commit -C HEAD -m 'fix: reuse'", ("fix: reuse", [])),
        ("git commit -v -m 'fix: verbose'", ("fix: verbose", [])),
        ("git commit -- -m x", None),
        ("git commit -am", None),
        ('git commit -m ""', None),
        ('git commit -m "Merge branch \'feature\' into main"', ("Merge branch 'feature' into main", [])),
        ("git commit -m 'Revert \"feat: add x.\"'", ('Revert "feat: add x."', [])),
        ('git commit -m "fixup! feat: add app"', ("fixup! feat: add app", [])),
        ('git commit -m "squash! fixup! feat: add app"', ("squash! fixup! feat: add app", [])),
        ('git commit -m "amend! Feat: Bad."', ("amend! Feat: Bad.", ["format", "punctuation"])),
        ('git commit -m "Merge the two parsers"', ("Merge the two parsers", ["format"])),
        ('git commit -m "fix: API key parsing"', ("fix: API key parsing", [])),
        ('git commit -m "docs: GitHub link"', ("docs: GitHub link", [])),
        ('git commit -m "fix(mem): Windows の repo root を直す"', ("fix(mem): Windows の repo root を直す", [])),
        ('git commit -m "fix: 3 files"', ("fix: 3 files", [])),
        ('git commit -m "docs: 説明を直す。"', ("docs: 説明を直す。", ["punctuation"])),
        ('git commit -m "docs: 説明を直す．"', ("docs: 説明を直す．", ["punctuation"])),
    ],
    ids=[
        "not-a-commit",
        "lowercase-no-period",
        "capitalized-with-period",
        "unconventional-long-with-period",
        "heredoc-command-substitution",
        "backtick-command-substitution",
        "body-is-not-the-subject",
        "combined-all-and-message",
        "combined-quiet-and-message",
        "attached-value",
        "long-option-with-equals",
        "long-option-separate-value",
        "apostrophe-inside-double-quotes",
        "breaking-change-first-message-only",
        "breaking-change-with-scope",
        "value-of-another-short-option",
        "message-after-another-option-value",
        "message-after-flag-without-value",
        "after-end-of-options",
        "missing-value",
        "empty-message",
        "git-generated-merge",
        "git-generated-revert",
        "autosquash-fixup",
        "autosquash-stacked",
        "autosquash-checks-the-subject-behind-the-marker",
        "hand-written-merge-wording",
        "acronym-cannot-be-lowercased",
        "name-with-inner-capital",
        "leading-name-in-japanese-subject",
        "leading-number",
        "japanese-full-stop",
        "fullwidth-period",
    ],
)
def test_validate_commit_message(command: str, expected: tuple[str, list[str]] | None) -> None:
    """commit 以外とコマンド置換の本文は None、commit はメッセージと件名行への指摘種別を返すこと。

    指摘種別は conventional 形式・72 文字超・大文字始まり・末尾のピリオドと句点（`.` `。` `．`）で、どれも 1 行目だけを見る（本文の
    長さや本文末尾の句点で件名を咎めない）。メッセージはトークン化した引数から読み、結合短形式（`-am`）・
    値の結合（`-mmsg`）・二重引用符の中の `'` も値どおりに読む。コマンド置換は本文が実行時に
    しか決まらず、字面（`$(cat <<`）を本文として検査すると毎回の commit で形式違反の警告が出る。
    git が生成する件名（`Merge branch …` / `Revert "…"`）は咎めず、autosquash の印は外して後ろの件名を見る。
    大文字始まりは英語だけの説明部の先頭語が `Add` の形のときだけ咎め、略語（`API`）・内に大文字を含む
    名前（`GitHub`）・日本語の件名の先頭の英単語（`Windows の…`）は咎めない。
    """
    result = commit_message.validate_commit_message(command)

    summary = None if result is None else (result["message"], [issue["type"] for issue in result["issues"]])
    assert summary == expected
