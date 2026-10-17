"""claq.hooks.pre_bash_commit_quality が commit 種別ごとに検査するファイルの範囲のテスト（--amend・-a の未ステージ変更・未ステージ変更の取得）。"""

from __future__ import annotations

import itertools
import subprocess
from pathlib import Path

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality

from ._support import _spy_commit_message_validation


def test_evaluate_commit_dash_a_shares_secret_scan_budget_with_worktree_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git commit -a` の INDEX 側と作業ツリー側でも予算は 1 本を共有すること。

    `_count_file_issues` はフック 1 回につき INDEX 用と作業ツリー用の 2 回
    呼ばれるため、そこで deadline を作ると予算が 2 本になり同じ穴が半分
    残る。作業ツリー側のファイルが INDEX 側で使い切った予算を引き継いで
    scan_error になることを固定する。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    clock = itertools.count(0.0, 9.0)
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "some text")
    monkeypatch.setattr(
        commit_quality_scanner, "get_worktree_file_content", lambda repo_root, path: "some text"
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): add'"}},
    )
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged1.js", "src/staged2.js"]
    )
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_unstaged_modified_files", lambda: ["src/unstaged.js"]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: Path("/dummy/repo"))
    validated = _spy_commit_message_validation(monkeypatch)

    result = pre_bash_commit_quality.evaluate("payload")

    # INDEX 側 2 件目（累積 18 秒）で予算が尽き、作業ツリー側（累積 27 秒）も
    # 同じ予算を見るため scan_error になる。予算が 2 本なら後者は通ってしまう。
    assert result["exitCode"] == 2
    assert any(
        "src/unstaged.js" in message and "SecretScanBudgetExceeded" in message for message in logs
    )
    assert validated == ["git commit -am 'feat(core): add'"]


def test_evaluate_commit_amend_with_nothing_staged_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--amend --no-edit` で staged が空なら検査対象ゼロで通過する（正常系。gap ではない）。

    連続空白を含む --amend コマンドでも commit 検出が成立することも併せて確認。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git   commit  --amend --no-edit"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("No staged files found" in message for message in logs)


def test_evaluate_commit_amend_scans_staged_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`--amend` は HEAD のファイル一覧を巻き込まず、staged 集合だけを検査する。

    --amend を検出した時点で lint/secret/message 検証をスキップすると、
    amend でこっそり secret を混入させても検査を通過できてしまう。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit --amend -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "should_scan_secrets", lambda path: False)

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
        seen.append(path)
        return [{"type": "secret", "severity": "error", "message": "hardcoded secret detected", "line": 1}]

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)

    result = pre_bash_commit_quality.evaluate("payload")

    # staged 集合は検査され、amend であることを理由にブロックが回避されない。
    assert seen == ["src/staged.py"]
    assert result["exitCode"] == 2


def test_evaluate_commit_dash_a_unions_unstaged_modified_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git commit -a` では未ステージの変更ファイルもスキャン対象に加わり、
    その分は作業ツリー（repo_root 経由）から読まれること
    （INDEX を読んで機能していなかった実欠陥の回帰防止）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_unstaged_modified_files", lambda: ["src/unstaged.py"]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)

    dummy_repo_root = Path("/dummy/repo")
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: dummy_repo_root)

    seen: list[tuple[str, Path | None]] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
        seen.append((path, repo_root))
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert dict(seen) == {"src/staged.py": None, "src/unstaged.py": dummy_repo_root}
    assert validated == ["git commit -am 'feat(core): add'"]


def test_evaluate_commit_dash_a_worktree_blocked_when_repo_root_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo root が解決できない場合、-a の未ステージ分は fail-closed で deny されること。

    `-a` で実際にコミットされる未ステージ変更を一切検査できないまま
    「問題なし」を返すと、get_staged_files() 側で塞いでいる fail-open と
    同じ穴が worktree 側に残る。staged 側と対称に fail-closed にする。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality, "get_unstaged_modified_files", lambda: ["src/unstaged.py"]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "resolve_repo_root", lambda: None)

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    # index 側は検査済みだが worktree 側（未ステージ分）には repo root 解決前に到達しない
    assert seen == ["src/staged.py"]
    assert any("could not resolve repo root" in message for message in logs)


def test_evaluate_commit_without_dash_a_ignores_unstaged_modified_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-a` の無い通常コミットでは未ステージの変更ファイルを取得しないこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/staged.py"])
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "get_unstaged_modified_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None, deadline: [])
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert validated == ["git commit -m 'feat(core): add'"]


def _init_repo_with_initial_commit(repo: Path, initial_content: str) -> Path:
    """テスト用に実 git リポジトリを初期化し、app.py を1回コミットする。"""
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
    app_py = repo / "app.py"
    app_py.write_text(initial_content, encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "feat(core): initial"], cwd=repo, check=True, capture_output=True)
    return app_py


def test_evaluate_commit_dash_a_detects_secret_in_unstaged_worktree_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`git commit -a` は未ステージの作業ツリー変更を読み、新規シークレットを
    検出すること（INDEX（git show :file）を読んでいたため未ステージの
    シークレットが検出できなかった実欠陥の回帰防止。空サンドボックスで
    CRITICAL として実証済みのシナリオ）。"""
    repo = tmp_path / "repo"
    app_py = _init_repo_with_initial_commit(repo, "value = 'clean'\n")

    # 作業ツリーのみ変更（git add しない）— INDEX は "clean" のまま
    secret_line = "api" + "_key" + ' = "hunter2secret"'
    app_py.write_text("value = 'clean'\n" + secret_line + "\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -am 'feat(core): update'"}},
    )

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2


def test_evaluate_commit_without_dash_a_reads_index_when_worktree_diverges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-a` を伴わない通常コミットでは、ステージ後に作業ツリーがさらに変更
    されても INDEX（ステージ済み内容）のシークレットを検出すること
    （読み取り元が誤って作業ツリーに切り替わっていないことの回帰防止＝
    staged 経路の不変性確認）。"""
    repo = tmp_path / "repo"
    app_py = _init_repo_with_initial_commit(repo, "value = 'clean'\n")

    secret_line = "api" + "_key" + ' = "hunter2secret"'
    app_py.write_text("value = 'clean'\n" + secret_line + "\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=repo, check=True, capture_output=True)

    # ステージ後、作業ツリーだけをさらに変更して secret を除去（再ステージしない）
    app_py.write_text("value = 'clean'\n", encoding="utf-8")

    monkeypatch.chdir(repo)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): update'"}},
    )

    result = pre_bash_commit_quality.evaluate("payload")

    # INDEX（ステージ済み secret 版）を読んで検出する。誤って作業ツリー
    # （secret 除去後）を読んでいれば exitCode は 0 になってしまう。
    assert result["exitCode"] == 2


def test_get_unstaged_modified_files_returns_success_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """`git diff HEAD` の成功出力からファイル一覧を返すこと。"""

    def fake_run(command: list[str], **kwargs: object):
        assert command == ["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMRT"]
        # `_git_name_only` は `subprocess_utils.run_text` 経由で呼ぶため、
        # `encoding` / `errors` / `env` / `input` も渡ってくる。個別に受けると
        # 呼び出し側の引数が増えるたびにテストが壊れるので **kwargs で受ける。
        assert kwargs["encoding"] == "utf-8"
        assert kwargs["errors"] == "replace"
        return subprocess.CompletedProcess(command, 0, stdout="src/a.py\nsrc/b.py\n", stderr="")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["src/a.py", "src/b.py"]


def _fake_git(results: dict[tuple[str, ...], subprocess.CompletedProcess]):
    """argv の先頭 3 語をキーに CompletedProcess を返す subprocess.run の差し替えを作る。"""

    def fake_run(argv, *args, **kwargs):
        return results[tuple(argv[:3])]

    return fake_run


def test_get_unstaged_modified_files_returns_empty_when_head_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """初回コミット（HEAD 不在）で diff が失敗した場合は空リストを返すこと。

    HEAD が無いのは正常系なので、ここだけは fail-open のままにする。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 1, stdout="", stderr=""),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() == []


def test_get_unstaged_modified_files_returns_none_when_git_fails_with_head(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD はあるのに diff が失敗した場合は None を返すこと（fail-closed）。

    「対象ファイルなし」と「検査できなかった」を区別しないと、実際にコミット
    される未ステージ変更を 1 件も検査せずに通してしまう（docs/adr/hook-failure-direction.md）。
    """
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 0, stdout="abc123\n", stderr=""),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None


def test_get_unstaged_modified_files_returns_none_when_head_check_also_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEAD の有無自体を判定できない場合も None を返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        _fake_git(
            {
                ("git", "diff", "HEAD"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
                ("git", "rev-parse", "--verify"): subprocess.CompletedProcess([], 128, stdout="", stderr="fatal"),
            }
        ),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None


def test_get_unstaged_modified_files_returns_none_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess が OSError 系例外を投げた場合は None を返すこと（fail-closed）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() is None
