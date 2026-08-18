"""`pre_bash_commit_quality` 専用テスト（A-01 shell 区切り誤認 / A-05 相当の fail-open 境界）。

`test_hook_edge_cases.py` に相乗りしていた `_is_git_commit_command` 系テストのうち、
shell 区切り文字の密着トークン化（A-01）と stdin truncation / main() の
fail-open・fail-closed 境界分割（A-05 相当、後続コミットで追加）に関する分岐は
本ファイルへ集約する。
"""

from __future__ import annotations

import pytest

from bluecore.hooks import pre_bash_commit_quality as pbcq


class TestShellSeparatorTokenization:
    """A-01: shell 区切り文字がトークンへ密着した非 commit コマンドの誤検出防止。"""

    @pytest.mark.parametrize(
        ("command", "expected_is_commit"),
        [
            # 区切り文字が密着（空白なし）していても、区切りとして認識し
            # セグメントを分けること。"status;echo" は 1 トークンではない。
            ("git status;echo commit", False),
            ("git status&&echo commit", False),
            ("git status||echo commit", False),
            ("git status|echo commit", False),
            # 退行防止: 区切り後の本物の commit は捕まえ続けること。
            ("git status; git commit", True),
            ("git status && git commit", True),
            ("false && git commit", True),
            ("git add -A&&git commit", True),
            ("git add -A&&git commit -m x", True),
        ],
    )
    def test_is_git_commit_command_shell_separator_boundary(
        self, command: str, expected_is_commit: bool
    ) -> None:
        is_commit, _args = pbcq._is_git_commit_command(command)
        assert is_commit is expected_is_commit

    def test_regex_fallback_still_accepts_echo_git_commit(self) -> None:
        """`echo "git commit"` は安全側の誤検出として許容する（受容事項）。"""
        is_commit, _args = pbcq._is_git_commit_command('echo "git commit"')
        assert is_commit is True
