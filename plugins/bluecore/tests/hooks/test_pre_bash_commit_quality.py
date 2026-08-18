"""`pre_bash_commit_quality` 専用テスト（A-01 shell 区切り誤認 / A-05 相当の fail-open 境界）。

`test_hook_edge_cases.py` に相乗りしていた `_is_git_commit_command` 系テストのうち、
shell 区切り文字の密着トークン化（A-01）と stdin truncation / main() の
fail-open・fail-closed 境界分割（A-05 相当）に関する分岐は本ファイルへ集約する。
"""

from __future__ import annotations

import json

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


class TestMainStdinBoundary:
    """A-05相当: stdin truncation 検知と main() の fail-open/fail-closed 境界分割。"""

    def test_truncated_stdin_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1 MiB 超で切り捨てられた入力は commit 判定不能として deny する。"""
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation",
            lambda: ("{not-even-json", True),
        )

        assert pbcq.main() == 2
        err = capsys.readouterr().err
        assert "BLOCKED" in err
        assert "exceeded" in err

    def test_stdin_read_exception_before_commit_confirmed_is_fail_open(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """commit 確定前（stdin 読み取り自体）の例外は非ブロッキング（従来通り）。"""

        def _raise() -> tuple[str, bool]:
            raise RuntimeError("stdin failure")

        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin_with_truncation", _raise)

        assert pbcq.main() == 0

    def test_evaluate_exception_is_fail_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`evaluate()` は例外を投げない契約だが、防御的に例外時も fail-open で扱う。"""
        payload = json.dumps({"tool_input": {"command": "git commit -m x"}})
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation", lambda: (payload, False)
        )

        def _raise(_raw: str) -> dict:
            raise RuntimeError("evaluate failure")

        monkeypatch.setattr(pbcq, "evaluate", _raise)

        assert pbcq.main() == 0

    def test_emit_block_output_failure_after_commit_confirmed_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """commit と確定し exitCode=2 が決まった後の emit_block_output 失敗は fail-closed（exit 2）。"""
        # malformed JSON だが raw 文字列上に `git commit` が見える経路
        # (_MALFORMED_COMMIT_MESSAGE) は staged files 取得を経由せず
        # 即座に exitCode=2 を確定するため、ここでの検証に適する。
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation",
            lambda: ("not-json but has git commit in it", False),
        )

        def _raise(_reason: str) -> int:
            raise RuntimeError("emit failure")

        monkeypatch.setattr("bluecore.hooks.hook_common.emit_block_output", _raise)

        assert pbcq.main() == 2

    def test_truncation_emit_block_output_failure_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """truncation 検知後の emit_block_output 失敗も fail-closed（exit 2）。"""
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation",
            lambda: ("{not-even-json", True),
        )

        def _raise(_reason: str) -> int:
            raise RuntimeError("emit failure")

        monkeypatch.setattr("bluecore.hooks.hook_common.emit_block_output", _raise)

        assert pbcq.main() == 2

    def test_non_commit_command_still_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps({"tool_input": {"command": "git status"}})
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin_with_truncation", lambda: (payload, False)
        )

        assert pbcq.main() == 0
