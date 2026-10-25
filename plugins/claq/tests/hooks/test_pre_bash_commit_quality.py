"""`pre_bash_commit_quality` 専用テスト（shell 区切り誤認 / fail-open 境界）。

`_is_git_commit_command` 系テストのうち、shell 区切り文字の密着トークン化と
stdin truncation / main() の fail-open・fail-closed 境界分割に関する分岐は
本ファイルへ集約する。
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable

import pytest

from claq.hooks import block_no_verify, commit_risk, stdin_io
from claq.hooks import pre_bash_commit_quality as pbcq


class TestShellSeparatorTokenization:
    """shell 区切り文字がトークンへ密着した非 commit コマンド・引用文中の語の誤検出防止。"""

    @pytest.mark.parametrize(
        ("command", "expected_is_commit"),
        [
            # 区切り文字が密着（空白なし）していても、区切りとして認識し
            # セグメントを分けること。"status;echo" は 1 トークンではない。
            ("git status;echo commit", False),
            ("git status&&echo commit", False),
            ("git status||echo commit", False),
            ("git status|echo commit", False),
            # 解析できた引用文の `git commit` は commit ではない。
            ('echo "git commit"', False),
            # 退行防止: 区切り後の本物の commit は捕まえ続けること。
            ("git status; git commit", True),
            ("git status && git commit", True),
            ("false && git commit", True),
            ("git add -A&&git commit", True),
            ("git add -A&&git commit -m x", True),
        ],
    )
    def test_is_git_commit_command_shell_separator_boundary(self, command: str, expected_is_commit: bool) -> None:
        """区切り文字で分けたセグメントの実行位置にある `git commit` だけを commit と判定すること。"""
        is_commit, _args = commit_risk._is_git_commit_command(command)
        assert is_commit is expected_is_commit

    def test_block_no_verify_and_commit_quality_agree_on_quoted_text(self) -> None:
        """2 つのフックが「commit とは何か」で食い違わないこと。

        どの ADR もこの非対称を正当化していなかった。docs/adr/shell-analysis-boundary.md の
        「誤検出 > 誤通過」は解析できない構文についての規定であり、解析できた
        構文にまで適用する根拠にはならない。
        """
        quoted = "copilot -p 'Explain why a git commit command may fail'"

        assert commit_risk._is_git_commit_command(quoted)[0] is False
        assert block_no_verify.has_bypass_flag(quoted) is False


def _unreadable_stdin() -> tuple[str, bool]:
    """stdin を読めない状態（`StdinUnavailableError`）を再現する。"""
    raise stdin_io.StdinUnavailableError("boom")


# (stdin の読み取り, deny 理由)。main() で deny が決まる経路。
_DENIED_READS = [
    pytest.param(lambda: ("{not-even-json", True), pbcq._TRUNCATED_INPUT_MESSAGE, id="truncated"),
    # malformed JSON だが raw 文字列上に `git commit` が見える経路は、staged files 取得を経由せず即座に deny を確定する。
    pytest.param(
        lambda: ("not-json but has git commit in it", False), pbcq._MALFORMED_COMMIT_MESSAGE, id="commit-confirmed"
    ),
]
# 「読めない」を「入力なし」へ正規化して exit 0 にすると、Windows のパイプで allow と deny が同じ exit 0 になる
# （docs/adr/hook-failure-direction.md）。この経路の main() の出力は test_stdin_unavailable_fail_closed.py が
# 4 hook 揃えて固定するので、ここでは deny 確定後の出力層の失敗だけを見る。
_UNREADABLE_READ = pytest.param(
    _unreadable_stdin, stdin_io.stdin_unreadable_message("pre:bash-commit-quality", "boom"), id="stdin-unreadable"
)


class TestMainStdinBoundary:
    """stdin の切り捨て・読み取り不能の検知と main() の fail-open/fail-closed 境界分割。"""

    @pytest.mark.parametrize(
        ("read", "expected_reason"),
        [
            *_DENIED_READS,
            pytest.param(
                lambda: (json.dumps({"tool_input": {"command": "git status"}}), False), None, id="non-commit"
            ),
        ],
    )
    def test_main_exit_code_and_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        read: Callable[[], tuple[str, bool]],
        expected_reason: str | None,
    ) -> None:
        """deny は exit 2 と理由入りの deny JSON・stderr を出し、commit でないコマンドは品質スキャンに入らず無出力の exit 0 で通すこと。"""
        monkeypatch.setattr("claq.hooks.stdin_io.read_raw_stdin_with_truncation", read)

        rc = pbcq.main()
        captured = capsys.readouterr()

        if expected_reason is None:
            assert (rc, captured.out, captured.err) == (0, "", "")
        else:
            assert rc == 2
            assert json.loads(captured.out) == {"permissionDecision": "deny", "permissionDecisionReason": expected_reason}
            assert expected_reason in captured.err

    @pytest.mark.parametrize(("read", "expected_reason"), [*_DENIED_READS, _UNREADABLE_READ])
    def test_emit_block_output_failure_after_deny_is_fail_closed(
        self, monkeypatch: pytest.MonkeyPatch, read: Callable[[], tuple[str, bool]], expected_reason: str
    ) -> None:
        """deny が決まってからの emit_block_output 失敗（出力層の失敗）でも、その理由を渡したうえで exit 2 を保つこと。"""
        monkeypatch.setattr("claq.hooks.stdin_io.read_raw_stdin_with_truncation", read)
        reasons: list[str] = []

        def _raise(reason: str) -> int:
            """渡された理由を記録し、出力層の失敗を再現する。"""
            reasons.append(reason)
            raise RuntimeError("emit failure")

        monkeypatch.setattr("claq.hooks.hook_common.emit_block_output", _raise)

        assert pbcq.main() == 2
        assert reasons == [expected_reason]


class TestCompoundCommitGuard:
    """実行前状態では commit 内容を検査できない複合コマンドを deny する。"""

    @pytest.mark.parametrize(
        ("command", "expected_message"),
        [
            # 複数 commit: 2 つ目がコミットする内容は実行前状態に現れない。
            ("git commit --allow-empty -m first && git commit -am second", commit_risk._MULTIPLE_COMMITS_MESSAGE),
            ("git commit -m a; git commit -m b", commit_risk._MULTIPLE_COMMITS_MESSAGE),
            # commit より前の index / worktree 変更。
            ("git add . && git commit -m x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            (
                "printf '" + "password" + "=supersecret123\\n' > secret.py"
                " && git add secret.py && git commit -m x",
                commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE,
            ),
            ("rm old.py && git commit -am x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("sed -i 's/a/b/' f.py && git commit -m x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("git reset --soft HEAD~1 && git commit -m x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("cp a.py b.py && git commit -am x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # ファイルへのリダイレクトは `>&` でも作業ツリーへの書き込み。
            ("echo x >&f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x >& f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x &>f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x &>>f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x > f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x >> f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # `2x` は数字のみではなくファイル名。数字を含んでも fd 複製とは読まない。
            ("echo x >&2x && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # 全角数字は fd 番号ではない。
            ("echo x >&\uff12 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # fd 複製の免除は `>&` だけ。他の変更演算子の直後が数字・`-` でもファイル名なので変更扱い。
            ("echo x > 2 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x &> 2 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x >> 1 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x &>> 1 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x 2> 1 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x >| 3 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            ("echo x > - && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # 後続トークンが無い `>&` は書き込み先を判定できないため変更扱い。
            ("echo x >& ; git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # fd 複製の直後でない数字は巻き込まない: `>&` の次はファイル名 `f`。
            ("echo 1 >&f 2 && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # fd 複製の後ろに別の書き込みがあれば、そちらで変更扱い。
            ("cmd 2>&1 > f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # 解析不能な入力でも `>&file` は変更扱い。
            ("echo $'\\'' >&f && git commit -m a", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # 受容済みの誤検出: 解析不能な入力では引用文中の index 変更も実行構造として読む。
            ("echo $'\\''; echo \"git add -A\"; git commit -m x", commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
        ],
    )
    def test_uninspectable_compounds_are_denied(self, command: str, expected_message: str) -> None:
        """commit より前の変更・複数 commit を含む複合コマンドを、その理由で止めること。"""
        assert commit_risk._compound_commit_risk(command) == expected_message

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
            # fd の複製・クローズ（`>&` の直後が数字のみか `-`）は書き込みではない。
            "cmd 2>&1 && git commit -m x",
            "echo x >&2 && git commit -m a",
            "echo x >& 2 && git commit -m a",
            "cmd >&- && git commit -m x",
            "cmd 1>&2 && git commit -m x",
            "cmd 2>&1 | cat && git commit -m x",
            "echo 1 >&2 && git commit -m x",
            "echo $'\\'' >&2 && git commit -m a",
            # commit を含まないコマンドは、変更操作があっても本ガードの対象外。
            "git add . && git status",
        ],
    )
    def test_inspectable_commands_pass(self, command: str) -> None:
        """commit 前が read-only・commit が単独・commit を含まないコマンドは本ガードで止めないこと。"""
        assert commit_risk._compound_commit_risk(command) is None

    @pytest.mark.parametrize(
        "segment",
        [
            # サブコマンドの無い `git` 単体・オプションだけで終わる `git -C /tmp` は index を変更しない。
            ["git"],
            ["git", "-C", "/tmp"],
            # `sed --in-place` 以外の長オプションを `-i` と誤認しない。
            ["sed", "--expression", "s/a/b/", "f.py"],
        ],
        ids=["bare-git", "git-options-only", "sed-long-option"],
    )
    def test_segment_is_not_a_mutation(self, segment: list[str]) -> None:
        """作業ツリーも index も変えないセグメントを変更操作と判定しないこと。"""
        assert commit_risk._segment_mutates_worktree_or_index(segment) is False

    @pytest.mark.parametrize(
        ("payload", "expected_reason"),
        [
            ({"tool_input": {"command": "git add . && git commit -m x"}}, commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE),
            # 先勝ちで 1 キーだけ見ると、`tool_input` に無害なコマンドを置き `toolArgs` に
            # `git commit` を載せるだけで品質検査ごと素通りする。
            (
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "echo safe"},
                    "toolArgs": {"command": "git add . && git commit -m x"},
                },
                commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE,
            ),
            # 走査層だけ全キー化して評価層が先勝ちのままだと、単独なら exit 2 の payload が先頭へ通る
            # commit を足すだけで exit 0 になった（実測）。複数コンテナに commit が散る形は実行順が
            # 確定できないため deny する。
            (
                {
                    "tool_name": "Bash",
                    "tool_input": {"command": "git commit -m ok"},
                    "tool_args": {"command": "git add . && git commit -m x"},
                },
                commit_risk._MULTIPLE_COMMITS_MESSAGE,
            ),
        ],
        ids=["single-container", "harmless-first-key", "passing-commit-first-key"],
    )
    def test_evaluate_denies_across_every_container_key(self, payload: dict, expected_reason: str) -> None:
        """evaluate() は全コンテナキーの `git commit` を検査し、deny 理由と exitCode 2 を返すこと。"""
        result = pbcq.evaluate(json.dumps(payload))

        assert result["exitCode"] == 2
        assert result["reason"] == expected_reason

    def test_evaluate_skips_container_whose_commit_detection_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 コンテナの判定失敗で残りのコンテナを捨てない。"""
        calls: list[str] = []

        def _flaky(command: str) -> tuple[bool, list[str]]:
            """呼び出しを記録し、``boom`` だけ判定に失敗する。"""
            calls.append(command)
            if command == "boom":
                raise RuntimeError("tokenize exploded")
            return (False, [])

        monkeypatch.setattr(pbcq, "_is_git_commit_command", _flaky)
        raw = json.dumps(
            {"tool_name": "Bash", "tool_input": {"command": "boom"}, "tool_args": {"command": "echo ok"}}
        )

        result = pbcq.evaluate(raw)

        assert result["exitCode"] == 0
        assert calls == ["boom", "echo ok"]


class TestArgvGroupCommitCounting:
    """argv 配列は 1 本のコマンドとして数え、要素と結合の二重計上で誤拒否しない。"""

    @staticmethod
    def _run(command: object, monkeypatch: pytest.MonkeyPatch) -> tuple[dict, list[str]]:
        """品質スキャンを stub に差し替えて ``evaluate`` を実行し、スキャンに渡った command を返す。"""
        scanned: list[str] = []

        def _stub(raw_input: str, command: str, commit_args: list[str]) -> dict:
            """スキャンに渡った command を記録し、問題なしで通す。"""
            scanned.append(command)
            return {"output": raw_input, "exitCode": 0}

        monkeypatch.setattr(pbcq, "_evaluate_confirmed_commit", _stub)
        raw = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        return pbcq.evaluate(raw), scanned

    @pytest.mark.parametrize(
        ("command", "expected_exit", "expected_reason", "expected_scanned"),
        [
            # 引数に ``git commit`` を含む argv の commit は複数 commit として拒否しない。
            (
                ["git", "commit", "-m", "fix: git commit handling"],
                0,
                None,
                ["git commit -m 'fix: git commit handling'"],
            ),
            # ``bash -lc`` の引数に包まれた commit は要素側で検出し、品質スキャンを走らせる。
            (["bash", "-lc", "git commit -m 'bad'"], 0, None, ["git commit -m 'bad'"]),
            # argv を 2 本並べた配列は別々の commit として数え、複数 commit で deny する。
            (
                [["git", "commit", "-m", "a"], ["git", "commit", "-m", "b"]],
                2,
                commit_risk._MULTIPLE_COMMITS_MESSAGE,
                [],
            ),
            # コマンド文字列を並べた配列の複数 commit は要素をそのまま数えて deny する。
            (["git commit -m a", "git commit -m b"], 2, commit_risk._MULTIPLE_COMMITS_MESSAGE, []),
            # 結合側が検出しない配列でも、要素 1 件の commit は検査する。
            (["git commit -m a", "echo done"], 0, None, ["git commit -m a"]),
            # commit を含まない argv は検査せずに通す。
            (["git", "status", "--short"], 0, None, []),
        ],
        ids=[
            "message-mentions-git-commit",
            "shell-dash-c",
            "two-nested-argv",
            "two-flat-strings",
            "one-flat-string",
            "non-commit",
        ],
    )
    def test_argv_group_commit_count(
        self,
        monkeypatch: pytest.MonkeyPatch,
        command: list,
        expected_exit: int,
        expected_reason: str | None,
        expected_scanned: list[str],
    ) -> None:
        """argv 配列の commit を 1 本ずつ数え、1 本なら品質スキャンへ渡し、複数なら deny すること。"""
        result, scanned = self._run(command, monkeypatch)

        assert result["exitCode"] == expected_exit
        assert result.get("reason") == expected_reason
        assert scanned == expected_scanned

    def test_oversized_element_in_argv_group_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """グループ内の候補が走査予算を超えれば block する（候補単位の予算は維持）。"""
        monkeypatch.setattr(pbcq, "command_exceeds_scan_budget", lambda command: command == "big")
        result, scanned = self._run(["git", "big"], monkeypatch)

        assert result["exitCode"] == 2
        assert result["reason"] == pbcq._TOKEN_BUDGET_MESSAGE
        assert scanned == []

    def test_failing_candidate_in_argv_group_does_not_drop_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """グループ内 1 候補の判定失敗で、残りの候補の commit 検出を捨てない。"""
        real = pbcq._detect_git_commit

        def _flaky(command: str):
            """``boom`` だけ判定に失敗し、それ以外は本物の検出に任せる。"""
            if command == "boom":
                raise RuntimeError("tokenize exploded")
            return real(command)

        monkeypatch.setattr(pbcq, "_detect_git_commit", _flaky)
        result, scanned = self._run(["boom", "git commit -m a"], monkeypatch)

        assert result["exitCode"] == 0
        assert scanned == ["git commit -m a"]


class TestWorktreeEnumerationFailClosed:
    """`git commit -a` の作業ツリー列挙失敗を「対象なし」と区別する。"""

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


class TestStagedFileNameDecoding:
    """非 ASCII のステージ済みファイル名を例外なく扱えること。

    `text=True` だけで encoding を指定しないと、デコードは locale 依存になる。
    `LC_ALL=C`（preferred encoding = US-ASCII）の環境で日本語ファイル名を
    stage すると `UnicodeDecodeError` が except タプルを貫通し、docstring の
    「例外は発生しません」が破れてフック全体が落ちる。
    ファイル名は利用者が日常的に作るため、origin URL 由来の同種欠陥より
    発火確率が高い。
    """

    @pytest.mark.parametrize(
        ("returncode", "raw", "expected"),
        [
            (0, b"src/app.py\nsrc/tool.ts\n", ["src/app.py", "src/tool.ts"]),
            (0, "src/設定ファイル.py\nsrc/plain.py\n".encode(), ["src/設定ファイル.py", "src/plain.py"]),
            # `errors="replace"` でそのファイル名は置換文字へ潰れ内容を読めなくなるが、例外でフックごと
            # 落ちて commit が無検査で通る（fail-open）よりは、残りのファイルを検査できる方が良い。
            (0, b"src/\xff\xfe-broken.py\nsrc/plain.py\n", ["src/\ufffd\ufffd-broken.py", "src/plain.py"]),
            # git 自体が非0終了した場合は「0 件」ではなく None（検査不能）を返す。
            (1, b"", None),
        ],
        ids=["ascii", "non-ascii", "undecodable-bytes", "git-fails"],
    )
    def test_staged_file_names_are_decoded_as_utf8_under_c_locale(
        self, monkeypatch: pytest.MonkeyPatch, returncode: int, raw: bytes, expected: list[str] | None
    ) -> None:
        """US-ASCII ロケールでも `git diff` の出力を UTF-8（不正バイトは置換）で復号して返すこと。"""
        monkeypatch.setenv("LC_ALL", "C")
        monkeypatch.setenv("LANG", "C")

        def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            """`git diff` だけを受け付け、呼び出し側が渡した encoding / errors で ``raw`` を復号して返す。"""
            assert command[:2] == ["git", "diff"]
            assert kwargs["encoding"] == "utf-8"
            errors = kwargs["errors"]
            assert isinstance(errors, str)
            return subprocess.CompletedProcess(command, returncode, stdout=raw.decode("utf-8", errors), stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert pbcq.get_staged_files() == expected

    @pytest.mark.parametrize(
        "error",
        [PermissionError("git is not executable"), OSError("exec format error"), FileNotFoundError("missing")],
        ids=["permission-error", "exec-format-error", "git-missing"],
    )
    def test_os_errors_are_caught_as_unavailable(self, monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
        """git を起動できない OSError 系は None（検査不能）に落ちること。

        except が `FileNotFoundError` しか持たないと、`PermissionError`
        や実行形式不正は貫通する。
        """

        def fake_run(command: list[str], **kwargs: object):
            """git の起動失敗を再現する。"""
            raise error

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert pbcq.get_staged_files() is None
        assert pbcq._head_exists() is None


class TestCommitAgainstAnotherIndex:
    """フックが走査する index と別の index を commit する形を deny する。

    走査はフック起動時の cwd の index に対して行う。commit セグメントの
    ``-C`` / ``--git-dir`` / ``--work-tree`` や ``GIT_DIR=`` 等、commit より前の
    ``cd`` / ``pushd`` は実行時に別の index を commit させ、cwd に staged が無ければ
    未検査のまま exit 0 で通っていた。
    """

    @pytest.fixture(autouse=True)
    def _nothing_staged_in_cwd(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """cwd の index には staged が無い状態にする（素通りの再現条件）。"""
        monkeypatch.setattr(pbcq, "get_staged_files", lambda: [])

    @pytest.mark.parametrize(
        "command",
        [
            "git -C ../other commit -m x",
            "git --git-dir=../other/.git commit -m x",
            "git --git-dir ../other/.git commit -m x",
            "git --work-tree=../other commit -am x",
            "git --work-tree ../other commit -m x",
            "GIT_DIR=../other/.git git commit -m x",
            "GIT_WORK_TREE=../other git commit -am x",
            "GIT_INDEX_FILE=/tmp/idx git commit -m x",
            "env GIT_INDEX_FILE=/tmp/idx git commit -m x",
            "export GIT_DIR=../other/.git; git commit -m x",
            "cd ../other && git commit -m x",
            "cd ../other; git commit -m x",
            "pushd ../other && git commit -m x",
            "(cd ../other && git commit -m x)",
            "CD ../other && git commit -m x",
            # 実行位置ではない位置の `cd`（複合コマンド・組み込み経由）。
            "{ cd ../other; git commit -m x; }",
            "if cd ../other; then git commit -m x; fi",
            "builtin cd ../other; git commit -m x",
            # wrapper の作業ディレクトリ指定。
            "env --chdir=../other git commit -m x",
            "env --chdir ../other git commit -m x",
            "sudo -D ../other git commit -m x",
            # 名前だけの指定・追記代入・PowerShell の代入。
            "read GIT_DIR <<< ../other/.git; export GIT_DIR; git commit -m x",
            "printf -v GIT_INDEX_FILE /tmp/idx; git commit -m x",
            "GIT_INDEX_FILE+=/tmp/idx git commit -m x",
            "$env:GIT_DIR='../other/.git'; git commit -m x",
            # `--work-tree` と同じ効果の設定。
            "git -c core.worktree=../other commit -am x",
            "git -c CORE.WorkTree=../other commit -am x",
            "git --config-env=core.worktree=WT commit -am x",
            "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.worktree GIT_CONFIG_VALUE_0=../other git commit -am x",
            "GIT_CONFIG_PARAMETERS=x git commit -am x",
            "git config core.worktree ../other && git commit -am x",
        ],
    )
    def test_commit_against_another_index_is_denied(self, command: str) -> None:
        """別の index / 作業ツリーを commit しうる形は、cwd に staged が無くても deny する。"""
        result = pbcq.evaluate(json.dumps({"tool_input": {"command": command}}))

        assert result["exitCode"] == 2
        assert result["reason"] == pbcq._COMMIT_TARGET_UNVERIFIABLE_MESSAGE
        assert "separate tool call" in result["reason"]

    @pytest.mark.parametrize(
        "command",
        [
            "git commit -m x",
            "git -c user.name=x commit -m x",
            # commit 自身の `-C <commit>`（メッセージの再利用）はグローバルオプションではない。
            "git commit -C HEAD --amend",
            "git commit -m 'cd into GIT_DIR=x'",
            # commit より後ろの `cd` はその commit の対象を変えない。
            "git commit -m x && cd ..",
        ],
    )
    def test_commit_against_cwd_index_is_scanned(self, command: str) -> None:
        """cwd の index を commit する形は従来どおり走査へ進む（ここでは staged 無しで exit 0）。"""
        result = pbcq.evaluate(json.dumps({"tool_input": {"command": command}}))

        assert result["exitCode"] == 0

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo $'\\''; echo x-git commit", None),
            ("cd ../other; echo $'\\''; echo x-git commit", pbcq._COMMIT_TARGET_UNVERIFIABLE_MESSAGE),
        ],
        ids=["without-directory-change", "with-directory-change"],
    )
    def test_unparsable_commit_is_denied_only_with_directory_change(self, command: str, expected: str | None) -> None:
        """commit セグメントを特定できない（生文字列だけが commit を検出した）入力は、cd があれば deny し、無ければ走査へ進むこと。"""
        assert pbcq._commit_target_risk(command) == expected
