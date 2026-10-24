"""claq.hooks.pre_bash_commit_quality が commit 種別ごとに検査するファイルの範囲のテスト（--amend・-a の未ステージ変更・未ステージ変更の取得）。"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality

from ._support import _evaluate_in_repo, _git, _init_git_repo, _spy_commit_message_validation

_DASH_A = "git commit -am 'feat(core): add'"
_DUMMY_ROOT = Path("/dummy/repo")
_SECRET_ISSUE = {"type": "secret", "severity": "error", "message": "hardcoded secret detected", "line": 1}


def _stub_commit(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    staged: list[str],
    unstaged: list[str] | None = None,
    repo_root: Path | None = _DUMMY_ROOT,
) -> list[str]:
    """``command`` を受け取り staged / unstaged を列挙して ``repo_root`` を解決する状態にし、ログの記録を返す。

    ``unstaged`` が None なら、未ステージ変更の取得は呼ばれた時点で失敗する（`-a` の無い commit 用）。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "parse_json_object", lambda raw: {"tool_input": {"command": command}})
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: staged)

    def _unstaged() -> list[str]:
        """``unstaged`` を返す。None なら呼ばれたこと自体を失敗にする。"""
        assert unstaged is not None, "should not be called"
        return unstaged

    monkeypatch.setattr(pre_bash_commit_quality, "get_unstaged_modified_files", _unstaged)
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: repo_root)
    return logs


def test_evaluate_commit_dash_a_shares_secret_scan_budget_with_worktree_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git commit -a` の INDEX 側と作業ツリー側でも予算は 1 本を共有すること。

    `_count_file_issues` はフック 1 回につき INDEX 用と作業ツリー用の 2 回
    呼ばれるため、そこで deadline を作ると予算が 2 本になり同じ穴が半分
    残る。作業ツリー側のファイルが INDEX 側で使い切った予算を引き継いで
    scan_error になることを固定する。
    """
    logs = _stub_commit(monkeypatch, _DASH_A, ["src/staged1.js", "src/staged2.js"], ["src/unstaged.js"])
    clock = itertools.count(0.0, 9.0)
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "some text")
    monkeypatch.setattr(commit_quality_scanner, "get_worktree_file_content", lambda repo_root, path: "some text")
    validated = _spy_commit_message_validation(monkeypatch)

    result = pre_bash_commit_quality.evaluate("payload")

    # INDEX 側 2 件目（累積 18 秒）で予算が尽き、作業ツリー側（累積 27 秒）も
    # 同じ予算を見るため scan_error になる。予算が 2 本なら後者は通ってしまう。
    assert result["exitCode"] == 2
    assert any("src/unstaged.js" in message and "SecretScanBudgetExceeded" in message for message in logs)
    assert validated == [_DASH_A]


def test_evaluate_commit_amend_with_nothing_staged_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--amend --no-edit` で staged が空なら検査対象ゼロで通過する（正常系。gap ではない）。

    連続空白を含む --amend コマンドでも commit 検出が成立することも併せて確認。
    """
    logs = _stub_commit(monkeypatch, "git   commit  --amend --no-edit", [])

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("No staged files found" in message for message in logs)


def _record_scans(monkeypatch: pytest.MonkeyPatch, issues: list[dict]) -> list[tuple[str, Path | None]]:
    """``find_file_issues`` を、検査したパスと読み取り元（作業ツリーなら repo_root）を記録して ``issues`` を返す偽物に替える。"""
    seen: list[tuple[str, Path | None]] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
        """検査したパスと読み取り元を記録し、``issues`` を返す。"""
        seen.append((path, repo_root))
        return issues

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    return seen


@pytest.mark.parametrize(
    ("command", "unstaged", "expected_seen"),
    [
        # `-a` では未ステージの変更ファイルも加わり、その分は作業ツリー（repo_root 経由）から読む
        # （INDEX を読んで機能していなかった実欠陥の回帰防止）。
        (_DASH_A, ["src/unstaged.py"], [("src/staged.py", None), ("src/unstaged.py", _DUMMY_ROOT)]),
        # `-a` の無い通常コミットでは未ステージの変更ファイルを取得しない。
        ("git commit -m 'feat(core): add'", None, [("src/staged.py", None)]),
    ],
    ids=["dash-a", "without-dash-a"],
)
def test_evaluate_scans_the_files_the_commit_includes(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    unstaged: list[str] | None,
    expected_seen: list[tuple[str, Path | None]],
) -> None:
    """commit が含むファイルだけを、INDEX 側と作業ツリー側の正しい読み取り元で検査して通すこと。"""
    _stub_commit(monkeypatch, command, ["src/staged.py"], unstaged)
    seen = _record_scans(monkeypatch, [])
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert seen == expected_seen
    assert validated == [command]


@pytest.mark.parametrize(
    ("command", "repo_root", "issues", "expected_reason", "expected_log"),
    [
        # `--amend` は HEAD のファイル一覧を巻き込まず staged 集合だけを検査し、amend であることを
        # 理由に lint/secret/message 検証をスキップしない（amend で secret を混入させる抜け道）。
        (
            "git commit --amend -m 'feat(core): add'",
            _DUMMY_ROOT,
            [_SECRET_ISSUE],
            "[Hook] BLOCKED: 1 error(s) found in staged files. Fix them before committing.",
            "Line 1: hardcoded secret detected",
        ),
        # repo root が解決できなければ、`-a` の未ステージ分を検査できないまま「問題なし」を返さず
        # fail-closed で deny する（staged 側と対称）。worktree 側には repo root 解決前に到達しない。
        (
            _DASH_A,
            None,
            [],
            pre_bash_commit_quality._REPO_ROOT_UNAVAILABLE_MESSAGE,
            "could not resolve repo root",
        ),
    ],
    ids=["amend-with-error", "dash-a-without-repo-root"],
)
def test_evaluate_denies_after_scanning_staged_files(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    repo_root: Path | None,
    issues: list[dict],
    expected_reason: str,
    expected_log: str,
) -> None:
    """staged 側を検査したうえで、error の検出や作業ツリー側の検査不能を理由に deny すること。"""
    logs = _stub_commit(monkeypatch, command, ["src/staged.py"], ["src/unstaged.py"], repo_root)
    seen = _record_scans(monkeypatch, issues)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result == {"output": "payload", "exitCode": 2, "reason": expected_reason}
    assert seen == [("src/staged.py", None)]
    assert any(expected_log in message for message in logs)


@pytest.mark.parametrize(
    ("command", "stage_then_clean_worktree"),
    [
        # `-a` は未ステージの作業ツリー変更を読む（INDEX を読んでいたため検出できなかった実欠陥。
        # 空サンドボックスで CRITICAL として実証済みのシナリオ）。
        ("git commit -am 'feat(core): update'", False),
        # `-a` の無い commit はステージ後に作業ツリーから secret を消しても INDEX を読んで検出する
        # （読み取り元が誤って作業ツリーへ切り替わっていないこと）。
        ("git commit -m 'feat(core): update'", True),
    ],
    ids=["dash-a-reads-worktree", "without-dash-a-reads-index"],
)
def test_evaluate_detects_secret_from_the_source_the_commit_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str, stage_then_clean_worktree: bool
) -> None:
    """実 git で、commit 種別が読む側（作業ツリーか INDEX か）に入れた secret を検出して止めること。"""
    repo = _init_git_repo(tmp_path / "repo", {"app.py": "value = 'clean'\n"})
    (repo / "app.py").write_text("value = 'clean'\n" + "api" + "_key" + ' = "hunter2secret"\n', encoding="utf-8")
    if stage_then_clean_worktree:
        _git(repo, "add", "app.py")
        (repo / "app.py").write_text("value = 'clean'\n", encoding="utf-8")

    assert _evaluate_in_repo(command, repo, monkeypatch)["exitCode"] == 2


def test_get_unstaged_modified_files_returns_success_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git diff HEAD` の成功出力からファイル一覧を返すこと。"""

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess:
        """`git diff HEAD` の引数と UTF-8（不正バイトは置換）の復号指定を確かめて一覧を返す。"""
        assert command == ["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMRT"]
        # `subprocess_utils.run_text` 経由で `env` / `input` 等も渡るため **kwargs で受ける。
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(command, 0, stdout="src/a.py\nsrc/b.py\n", stderr="")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["src/a.py", "src/b.py"]


@pytest.mark.parametrize(
    ("rev_parse", "expected"),
    [
        # 初回コミット（HEAD 不在）は正常系なので、ここだけは空リスト（fail-open）のままにする。
        (subprocess.CompletedProcess([], 1, stdout="", stderr=""), []),
        # HEAD はあるのに diff が失敗したら None（fail-closed）。「対象ファイルなし」と「検査できなかった」を
        # 区別しないと、コミットされる未ステージ変更を 1 件も検査せずに通す（docs/adr/hook-failure-direction.md）。
        (subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr=""), None),
        # HEAD の有無自体を判定できない場合も None。
        (subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"), None),
    ],
    ids=["head-absent", "head-present", "head-check-fails"],
)
def test_get_unstaged_modified_files_when_diff_fails(
    monkeypatch: pytest.MonkeyPatch, rev_parse: subprocess.CompletedProcess, expected: list[str] | None
) -> None:
    """`git diff HEAD` が失敗したとき、HEAD の有無で空リストと None（検査不能）を分けること。"""
    results = {
        ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
        ("git", "rev-parse", "--verify"): rev_parse,
    }
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess, "run", lambda argv, *args, **kwargs: results[tuple(argv[:3])]
    )

    assert pre_bash_commit_quality.get_unstaged_modified_files() == expected


def test_get_unstaged_modified_files_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess が OSError 系例外を投げた場合は None を返すこと（fail-closed）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None
