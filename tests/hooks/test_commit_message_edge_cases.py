"""claq.hooks.commit_message のコミットメッセージ検証のテスト。"""

from __future__ import annotations

import pytest

from claq.hooks import commit_message, commit_risk

_LONG_UNCONVENTIONAL = "bad message with no conventional format and a very long subject line that keeps going."


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("git status", None, id="not-a-commit"),
        pytest.param('git commit -m "feat: add new feature"', ("feat: add new feature", []), id="lowercase-no-period"),
        pytest.param('git commit -m "feat(core): Add feature."', ("feat(core): Add feature.", ["capitalization", "punctuation"]), id="capitalized-with-period"),
        pytest.param(f'git commit -m "{_LONG_UNCONVENTIONAL}"', (_LONG_UNCONVENTIONAL, ["format", "length", "punctuation"]), id="unconventional-long-with-period"),
        pytest.param("git commit -m \"$(cat <<'EOF'\nfix: x\nEOF\n)\"", None, id="heredoc-command-substitution"),
        pytest.param("git commit -m `cat msg.txt`", None, id="backtick-command-substitution"),
        pytest.param(f'git commit -m "fix: short subject\n\n{_LONG_UNCONVENTIONAL}"', (f"fix: short subject\n\n{_LONG_UNCONVENTIONAL}", []), id="body-is-not-the-subject"),
        pytest.param('git commit -am "Fix: Bad."', ("Fix: Bad.", ["format", "punctuation"]), id="combined-all-and-message"),
        pytest.param('git commit -qm "feat: quiet"', ("feat: quiet", []), id="combined-quiet-and-message"),
        pytest.param('git commit -m"feat: attached"', ("feat: attached", []), id="attached-value"),
        pytest.param('git commit --message="docs: long form"', ("docs: long form", []), id="long-option-with-equals"),
        pytest.param('git commit --message "docs: separate value"', ("docs: separate value", []), id="long-option-separate-value"),
        pytest.param('git commit -m "fix: don\'t crash"', ("fix: don't crash", []), id="apostrophe-inside-double-quotes"),
        pytest.param('git commit -m "feat!: drop python 3.11" -m "body."', ("feat!: drop python 3.11", []), id="breaking-change-first-message-only"),
        pytest.param('git commit -m "feat(api)!: remove v1"', ("feat(api)!: remove v1", []), id="breaking-change-with-scope"),
        pytest.param("git commit -Cmain", None, id="value-of-another-short-option"),
        pytest.param("git commit -C HEAD -m 'fix: reuse'", ("fix: reuse", []), id="message-after-another-option-value"),
        pytest.param("git commit -v -m 'fix: verbose'", ("fix: verbose", []), id="message-after-flag-without-value"),
        pytest.param("git commit -- -m x", None, id="after-end-of-options"),
        pytest.param("git commit -am", None, id="missing-value"),
        pytest.param('git commit -m ""', None, id="empty-message"),
        pytest.param('git commit -m "Merge branch \'feature\' into main"', ("Merge branch 'feature' into main", []), id="git-generated-merge"),
        pytest.param("git commit -m 'Revert \"feat: add x.\"'", ('Revert "feat: add x."', []), id="git-generated-revert"),
        pytest.param('git commit -m "fixup! feat: add app"', ("fixup! feat: add app", []), id="autosquash-fixup"),
        pytest.param('git commit -m "squash! fixup! feat: add app"', ("squash! fixup! feat: add app", []), id="autosquash-stacked"),
        pytest.param('git commit -m "amend! Feat: Bad."', ("amend! Feat: Bad.", ["format", "punctuation"]), id="autosquash-checks-the-subject-behind-the-marker"),
        pytest.param('git commit -m "Merge the two parsers"', ("Merge the two parsers", ["format"]), id="hand-written-merge-wording"),
        pytest.param('git commit -m "fix: API key parsing"', ("fix: API key parsing", []), id="acronym-cannot-be-lowercased"),
        pytest.param('git commit -m "docs: GitHub link"', ("docs: GitHub link", []), id="name-with-inner-capital"),
        pytest.param('git commit -m "fix(mem): Windows の repo root を直す"', ("fix(mem): Windows の repo root を直す", []), id="leading-name-in-japanese-subject"),
        pytest.param('git commit -m "fix: 3 files"', ("fix: 3 files", []), id="leading-number"),
        pytest.param('git commit -m "docs: 説明を直す。"', ("docs: 説明を直す。", ["punctuation"]), id="japanese-full-stop"),
        pytest.param('git commit -m "docs: 説明を直す．"', ("docs: 説明を直す．", ["punctuation"]), id="fullwidth-period"),
    ],
)
def test_validate_commit_message(command: str, expected: tuple[str, list[str]] | None) -> None:
    """commit 以外・メッセージ無し・コマンド置換の本文は指摘なし、commit はメッセージを読み件名行への指摘種別を返すこと。

    指摘種別は conventional 形式・72 文字超・大文字始まり・末尾のピリオドと句点（`.` `。` `．`）で、どれも 1 行目だけを見る（本文の
    長さや本文末尾の句点で件名を咎めない）。メッセージはトークン化した引数から読み、結合短形式（`-am`）・
    値の結合（`-mmsg`）・二重引用符の中の `'` も値どおりに読む。コマンド置換は本文が実行時に
    しか決まらず、字面（`$(cat <<`）を本文として検査すると毎回の commit で形式違反の警告が出る。
    git が生成する件名（`Merge branch …` / `Revert "…"`）は咎めず、autosquash の印は外して後ろの件名を見る。
    大文字始まりは英語だけの説明部の先頭語が `Add` の形のときだけ咎め、略語（`API`）・内に大文字を含む
    名前（`GitHub`）・日本語の件名の先頭の英単語（`Windows の…`）は咎めない。
    """
    commit_args = commit_risk.parse_git_commit_command(command)[1]

    issues = commit_message.validate_commit_message(commit_args)

    assert [issue["type"] for issue in issues] == ([] if expected is None else expected[1])
    if expected is not None:
        assert commit_message._message_from_args(commit_args) == expected[0]
