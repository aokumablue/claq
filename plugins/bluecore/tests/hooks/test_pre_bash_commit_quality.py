"""`pre_bash_commit_quality` 専用テスト（A-01 shell 区切り誤認 / A-05 相当の fail-open 境界）。

`test_hook_edge_cases.py` に相乗りしていた `_is_git_commit_command` 系テストのうち、
shell 区切り文字の密着トークン化（A-01）と stdin truncation / main() の
fail-open・fail-closed 境界分割（A-05 相当）に関する分岐は本ファイルへ集約する。
"""

from __future__ import annotations

import json

import pytest

from bluecore.hooks import block_no_verify
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

    def test_quoted_git_commit_is_not_a_commit(self) -> None:
        """解析できた引用文の `git commit` は commit と判定しないこと（F-08）。"""
        is_commit, _args = pbcq._is_git_commit_command('echo "git commit"')
        assert is_commit is False

    def test_block_no_verify_and_commit_quality_agree_on_quoted_text(self) -> None:
        """2 つのフックが「commit とは何か」で食い違わないこと。

        どの ADR もこの非対称を正当化していなかった。ADR-0002 の
        「誤検出 > 誤通過」は解析できない構文についての規定であり、解析できた
        構文にまで適用する根拠にはならない。
        """
        quoted = "copilot -p 'Explain why a git commit command may fail'"

        assert pbcq._is_git_commit_command(quoted)[0] is False
        assert block_no_verify.has_bypass_flag(quoted) is False


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


class TestCompoundCommitGuard:
    """F-05: 実行前状態では commit 内容を検査できない複合コマンドを deny する。"""

    @pytest.mark.parametrize(
        ("command", "expected_message"),
        [
            # 複数 commit: 2 つ目がコミットする内容は実行前状態に現れない。
            ("git commit --allow-empty -m first && git commit -am second", pbcq._MULTIPLE_COMMITS_MESSAGE),
            ("git commit -m a; git commit -m b", pbcq._MULTIPLE_COMMITS_MESSAGE),
            # commit より前の index / worktree 変更。
            ("git add . && git commit -m x", pbcq._MUTATION_BEFORE_COMMIT_MESSAGE),
            (
                "printf 'password=supersecret123\\n' > secret.py && git add secret.py && git commit -m x",
                pbcq._MUTATION_BEFORE_COMMIT_MESSAGE,
            ),
            ("rm old.py && git commit -am x", pbcq._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("sed -i 's/a/b/' f.py && git commit -m x", pbcq._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("git reset --soft HEAD~1 && git commit -m x", pbcq._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("cp a.py b.py && git commit -am x", pbcq._MUTATION_BEFORE_COMMIT_MESSAGE),
        ],
    )
    def test_uninspectable_compounds_are_denied(self, command: str, expected_message: str) -> None:
        assert pbcq._compound_commit_risk(command) == expected_message

    @pytest.mark.parametrize(
        "command",
        [
            # 単独 commit。
            "git commit -m x",
            "git commit -am x",
            # commit 前が read-only なら検査結果は実行時と一致する。
            "git status && git commit -m x",
            "echo hi && git commit -m x",
            "git diff --cached && git commit -m x",
            # `-i` の無い sed は標準出力へ流すだけで書き込まない。
            "sed 's/a/b/' f.py && git commit -m x",
            # commit より後ろの変更は、その commit の内容を変えない。
            "git commit -m x && rm tmp.py",
            # 引用文中の語は実行位置ではない。
            "git commit -m 'add . && commit'",
        ],
    )
    def test_inspectable_commands_pass(self, command: str) -> None:
        assert pbcq._compound_commit_risk(command) is None

    def test_non_commit_command_is_not_evaluated(self) -> None:
        """commit を含まないコマンドは、変更操作があっても本ガードの対象外。"""
        assert pbcq._compound_commit_risk("git add . && git status") is None

    def test_bare_git_without_subcommand_is_not_a_mutation(self) -> None:
        """サブコマンドの無い `git` 単体は index を変更しない。"""
        assert pbcq._segment_mutates_worktree_or_index(["git"]) is False

    def test_git_with_only_options_is_not_a_mutation(self) -> None:
        """オプションだけで終わる `git -C /tmp` は index を変更しない。"""
        assert pbcq._segment_mutates_worktree_or_index(["git", "-C", "/tmp"]) is False

    def test_long_option_is_not_mistaken_for_sed_inplace(self) -> None:
        """`sed --in-place` 以外の長オプションを `-i` と誤認しないこと。"""
        assert pbcq._segment_mutates_worktree_or_index(["sed", "--expression", "s/a/b/", "f.py"]) is False

    def test_evaluate_denies_mutation_before_commit(self) -> None:
        """evaluate() が deny 理由と exitCode 2 を返すこと。"""
        raw = json.dumps({"tool_input": {"command": "git add . && git commit -m x"}})
        result = pbcq.evaluate(raw)
        assert result["exitCode"] == 2
        assert result["reason"] == pbcq._MUTATION_BEFORE_COMMIT_MESSAGE


class TestWorktreeEnumerationFailClosed:
    """F-06(a): `git commit -a` の作業ツリー列挙失敗を「対象なし」と区別する。"""

    def test_partition_returns_none_when_enumeration_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """列挙が None を返したら分割結果も None になること。"""
        monkeypatch.setattr(pbcq, "get_unstaged_modified_files", lambda: None)
        assert pbcq._partition_commit_all_files(["a.py"], ["-a", "-m", "x"]) is None

    def test_evaluate_denies_when_worktree_enumeration_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """列挙不能のまま「問題なし」を返さず deny すること。"""
        monkeypatch.setattr(pbcq, "get_staged_files", lambda: ["a.py"])
        monkeypatch.setattr(pbcq, "get_unstaged_modified_files", lambda: None)
        raw = json.dumps({"tool_input": {"command": "git commit -am x"}})
        result = pbcq.evaluate(raw)
        assert result["exitCode"] == 2
        assert result["reason"] == pbcq._WORKTREE_FILES_UNAVAILABLE_MESSAGE
