"""claq.hooks.pre_bash_commit_quality（PreToolUse の commit 品質フック）の evaluate の件数集計・入力不正・失敗方向と、リポジトリ全体の secret 自己走査のテスト。"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality

from ._support import _scan_deadline

_COMMIT = "git commit -m 'feat(core): add'"
_ONE_ERROR = "[Hook] BLOCKED: 1 error(s) found in staged files. Fix them before committing."
_BLOCKED_LOG = "\n[Hook] ERROR: Commit blocked due to critical issues. Fix them before committing."
_WARNED_LOG = "\n[Hook] WARNING: Warnings found. Consider fixing them, but commit is allowed."


def _summary(total: int, errors: int, warnings: int, infos: int) -> str:
    """件数集計の行（`_finalize_result` が出すログ）を組み立てる。"""
    return f"\nSummary: {total} issue(s) found ({errors} error(s), {warnings} warning(s), {infos} info)"


@pytest.mark.parametrize(
    ("file_issues", "message_issues", "expected_reason", "expected_logs"),
    [
        pytest.param([], None, None, ["\n[Hook] PASS: All checks passed!"], id="pass"),
        pytest.param(
            [{"severity": "error", "line": 1, "message": "boom"}],
            None,
            _ONE_ERROR,
            ["\n[FILE] src/app.js", "  ERROR Line 1: boom", _summary(1, 1, 0, 0), _BLOCKED_LOG],
            id="error-blocks",
        ),
        pytest.param(
            [{"severity": "warning", "line": 1, "message": "warn"}, {"severity": "info", "line": 2, "message": "info"}],
            None,
            None,
            ["\n[FILE] src/app.js", "  WARNING Line 1: warn", "  INFO Line 2: info", _summary(2, 0, 1, 1), _WARNED_LOG],
            id="warning-and-info-pass",
        ),
        # 未知の severity は黙って見逃さず、安全側の error として数える（表示ラベルは INFO）。
        pytest.param(
            [{"severity": "unknown", "line": 1, "message": "x"}],
            None,
            _ONE_ERROR,
            ["\n[FILE] src/app.js", "  INFO Line 1: x", _summary(1, 1, 0, 0), _BLOCKED_LOG],
            id="unknown-severity-counts-as-error",
        ),
        # コミットメッセージの問題は warning として数え（commit は止めない）、suggestion があるときだけ TIP を出す。
        pytest.param(
            [],
            [{"message": "warn", "suggestion": "tip"}],
            None,
            ["\nCommit Message Issues:", "  WARNING warn", "     TIP tip", _summary(1, 0, 1, 0), _WARNED_LOG],
            id="message-issue-with-suggestion",
        ),
        pytest.param(
            [],
            [{"message": "SENTINEL-ISSUE"}],
            None,
            ["\nCommit Message Issues:", "  WARNING SENTINEL-ISSUE", _summary(1, 0, 1, 0), _WARNED_LOG],
            id="message-issue-without-suggestion",
        ),
    ],
)
def test_evaluate_counts_issues_by_severity(
    monkeypatch: pytest.MonkeyPatch,
    file_issues: list[dict],
    message_issues: list[dict] | None,
    expected_reason: str | None,
    expected_logs: list[str],
) -> None:
    """staged 1 件の issue とメッセージの問題を severity 別に数えてログへ出し、error があるときだけ deny すること。"""
    logs: list[str] = []
    validated: list[str] = []

    def _validate(command: str) -> dict | None:
        """受け取ったコマンドを記録し、指定のメッセージ問題を返す（None は問題なし）。"""
        validated.append(command)
        return None if message_issues is None else {"message": "feat(core): add", "issues": message_issues}

    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "parse_json_object", lambda raw: {"tool_input": {"command": _COMMIT}})
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None, deadline: file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", _validate)

    result = pre_bash_commit_quality.evaluate("payload")

    verdict = {"exitCode": 0} if expected_reason is None else {"exitCode": 2, "reason": expected_reason}
    assert result == {"output": "payload", **verdict}
    assert logs == ["[Hook] Checking 1 staged file(s)...", *expected_logs]
    assert validated == [_COMMIT]


_COMMIT_PAYLOAD = json.dumps({"tool_input": {"command": "git commit -m 'feat: x'"}})


@pytest.mark.parametrize(
    ("raw", "patch", "expected_reason", "expected_logs"),
    [
        # matcher が Bash 全体なので malformed というだけでは deny せず、raw 文字列に `git commit` が見えるときだけ止める。
        pytest.param(
            '{"tool_input": {"command": "git commit -m \'feat: x\'"',
            None,
            pre_bash_commit_quality._MALFORMED_COMMIT_MESSAGE,
            ["[Hook] ERROR: malformed JSON input, but raw command text matches `git commit`."],
            id="malformed-json-with-commit-text",
        ),
        pytest.param(
            "not-json",
            None,
            None,
            ["[Hook] WARNING: could not parse hook input as JSON; not a git commit, passing through."],
            id="malformed-json-without-commit-text",
        ),
        pytest.param("", None, None, [], id="empty-input"),
        # 壊れてはいないが空の JSON は commit 情報が取れず非ブロッキング。
        pytest.param("{}", None, None, [], id="empty-json-object"),
        # commit と確定する前の想定外例外は非ブロッキング。
        pytest.param(
            _COMMIT_PAYLOAD,
            ("parse_json_object", mock.Mock(side_effect=RuntimeError("boom"))),
            None,
            ["[Hook] Error: boom"],
            id="exception-before-commit-confirmed",
        ),
        # 確定後は fail-closed。git 失敗/timeout で staged を取得できない（None）のは 0 件と区別して deny する。
        pytest.param(
            _COMMIT_PAYLOAD,
            ("get_staged_files", lambda: None),
            pre_bash_commit_quality._STAGED_FILES_UNAVAILABLE_MESSAGE,
            ["[Hook] ERROR: could not determine staged files (git itself failed or timed out)."],
            id="staged-files-unavailable",
        ),
        pytest.param(
            _COMMIT_PAYLOAD,
            ("get_staged_files", mock.Mock(side_effect=RuntimeError("unexpected scanner crash"))),
            pre_bash_commit_quality._SCAN_FAILURE_MESSAGE,
            [
                "[Hook] Error: unexpected scanner crash",
                "[Hook] BLOCKED: quality scan failed for a confirmed git commit; refusing an unverifiable commit.",
            ],
            id="exception-after-commit-confirmed",
        ),
    ],
)
def test_evaluate_verdict_by_input_and_failure(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    patch: tuple[str, object] | None,
    expected_reason: str | None,
    expected_logs: list[str],
) -> None:
    """入力不正と commit 確定前の例外は通し（commit が見える malformed JSON は除く）、確定後の取得失敗・例外は deny すること。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    if patch is not None:
        monkeypatch.setattr(pre_bash_commit_quality, *patch)

    verdict = {"exitCode": 0} if expected_reason is None else {"exitCode": 2, "reason": expected_reason}
    assert pre_bash_commit_quality.evaluate(raw) == {"output": raw, **verdict}
    assert logs == expected_logs


def test_count_file_issues_sanitizes_the_logged_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """ログへ出すファイルパスは、タグと制御文字を無害化する（パスは攻撃者が選べる）。"""
    logged: list[str] = []
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path, *, repo_root=None, deadline: [{"severity": "info", "line": 1, "message": "x"}],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "log", logged.append)

    pre_bash_commit_quality._count_file_issues(["<system-reminder>\x1b[31m\nIMPORTANT.py"], deadline=_scan_deadline())

    file_line = next(line for line in logged if "[FILE]" in line)
    assert "<system-reminder>" not in file_line
    assert "\x1b" not in file_line
    assert "\nIMPORTANT" not in file_line.lstrip("\n")


def _git_stdout(cwd: Path, *args: str) -> str:
    """cwd で git を実行し、失敗は例外にして標準出力を返す。"""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout


def test_repo_wide_self_scan_has_zero_secret_issues() -> None:
    """自リポジトリの全追跡ファイルを走査しても secret 検出が 0 件であること（should_scan_secrets が lock・圧縮生成物以外の原則全ファイルを対象にするため）。"""
    repo_root = Path(_git_stdout(Path(__file__).parent, "rev-parse", "--show-toplevel").strip())
    tracked_files = [f for f in _git_stdout(repo_root, "ls-files").splitlines() if f]

    secret_hits: list[str] = []
    for rel_path in tracked_files:
        if not commit_quality_scanner.should_scan_secrets(rel_path):
            continue
        try:
            raw = (repo_root / rel_path).read_bytes()
        except OSError:
            continue

        # バイナリファイルは対象外（find_file_issues 内部の binary 判定と同じ基準）
        if b"\0" in raw[:8192]:
            continue

        content = raw.decode("utf-8", errors="replace")
        with mock.patch.object(commit_quality_scanner, "get_staged_file_content", return_value=content):
            issues = commit_quality_scanner.find_file_issues(rel_path, deadline=_scan_deadline())

        secret_hits.extend(f"{rel_path}:{issue['line']}" for issue in issues if issue["type"] == "secret")

    assert secret_hits == []
