"""claq.hooks.pre_bash_commit_quality（PreToolUse の commit 品質フック）の端の挙動のテスト（main・evaluate の分岐・入力不正・件数集計）と、リポジトリ全体の secret 自己走査のテスト。"""

from __future__ import annotations

import io
import itertools
import json
import runpy
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import commit_message as commit_message
from claq.hooks import commit_quality_scanner as commit_quality_scanner
from claq.hooks import pre_bash_commit_quality as pre_bash_commit_quality
from claq.hooks import stdin_io as stdin_io

from ._support import _scan_deadline, _spy_commit_message_validation


def test_evaluate_shares_one_secret_scan_budget_across_staged_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """secret scan の実時間予算はフック 1 回の起動全体で共有されること。

    予算がファイル単位だと、1 ファイルあたり予算未満（ここでは 9 秒）で
    済む限り何ファイルでも走査でき、staged が N 件あれば
    N×`_SECRET_SCAN_TIME_BUDGET_SECONDS` 秒まで走ってしまう。それでは
    `commit_quality_scanner` のモジュール docstring が主張する
    「hook timeout（30秒）に達しない」を満たせない（1 ファイル 9 秒
    ×5 ファイルで累積 45 秒でも予算超過が 0 件になる）。

    エントリ（`evaluate`）から駆動して固定する。`find_file_issues` を
    直接呼ぶテストでは deadline の伝播が外れても気付けないため。

    予算は lint scan とも共有する 1 本なので、1 ファイルあたりの時刻取得は
    lint / secret の 2 回になる。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    clock = itertools.count(0.0, 9.0)
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "some text")
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    staged = [f"src/app{index}.js" for index in range(1, 6)]
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: staged)
    validated = _spy_commit_message_validation(monkeypatch)

    result = pre_bash_commit_quality.evaluate("payload")

    # 予算 10 秒は 1 ファイル目の secret scan（累積 18 秒）で尽き、以降は
    # 2〜5 ファイル目の lint scan が同じ 1 本の予算を見て即 scan_error になる。
    # 全件が scan_error（severity error、fail-closed）。
    # 予算がファイル単位なら 1 件も超過しない（1 ファイルあたり 9 秒 < 10 秒）。
    assert result["exitCode"] == 2
    exceeded = [
        path
        for path in staged
        if any(path in message and "SecretScanBudgetExceeded" in message for message in logs)
    ]
    assert exceeded == staged
    assert validated == ["git commit -m 'feat(core): add'"]


def test_evaluate_scans_non_lint_extension_files_for_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate() が lint 非対象拡張子（.env 等）もステージ済みファイルの走査対象に含めること。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [".env", "package-lock.json"])

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None, deadline: float) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    # .env は secret スキャン対象なので走査される。package-lock.json は
    # lint 非対象・secret 対象外の双方であるため走査対象から除外される。
    assert seen == [".env"]
    assert validated == ["git commit -m 'feat(core): add'"]


def test_pre_bash_commit_quality_finds_parser_and_reading_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """ファイル読み取り自体が例外を投げても、握り潰さず scan_error として報告する。

    get_staged_file_content は自前で例外を握り潰し None を返す契約だが、
    find_file_issues 側でもその契約破りに備えたガードを持つ。全体を
    except Exception: pass で覆って無条件に [] を返すと、検査したのか・
    対象外だったのかが区別できない。
    """
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())
    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"
    assert "RuntimeError" in issues[0]["message"]

    long_message = "git commit -m \"bad message with no conventional format and a very long subject line that keeps going.\""
    parsed = commit_message.validate_commit_message(long_message)
    assert parsed is not None
    assert {issue["type"] for issue in parsed["issues"]} >= {"format", "length"}


def test_pre_bash_commit_quality_main_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "claq.hooks.stdin_io.read_raw_stdin_with_truncation", lambda: ("payload", False)
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "evaluate",
        lambda raw: {"output": raw, "exitCode": 0},
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert pre_bash_commit_quality.main() == 0

    assert stdout.getvalue() == ""

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "evaluate",
        lambda raw: {"output": raw, "exitCode": 2, "reason": "[Hook] BLOCKED: boom"},
    )
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert pre_bash_commit_quality.main() == 2

    # ブロック時は stdout に permissionDecision: deny の合併 JSON を出す。
    deny = json.loads(stdout.getvalue())
    assert deny["permissionDecision"] == "deny"


def test_pre_bash_commit_quality_evaluate_handles_commit_branches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git status"}},
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "get_staged_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [])
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("No staged files found" in message for message in logs)


def test_pre_bash_commit_quality_blocks_on_error_and_allows_warnings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path, *, repo_root=None, deadline: [{"severity": "error", "line": 1, "message": "boom"}],
    )
    validated = _spy_commit_message_validation(monkeypatch)
    assert pre_bash_commit_quality.evaluate("payload") == {
        "output": "payload",
        "exitCode": 2,
        "reason": "[Hook] BLOCKED: 1 error(s) found in staged files. Fix them before committing.",
    }
    assert validated == ["git commit -m 'feat(core): add'"]

    warning_logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", warning_logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None, deadline: [])
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "validate_commit_message",
        lambda command: {
            "message": "feat(core): Add feature.",
            "issues": [{"message": "warn", "suggestion": "tip"}],
        },
    )
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("Commit Message Issues" in message for message in warning_logs)
    assert any("WARNING" in message for message in warning_logs)


def test_pre_bash_commit_quality_counts_warning_and_info_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path, *, repo_root=None, deadline: [
            {"severity": "warning", "line": 1, "message": "warn"},
            {"severity": "info", "line": 2, "message": "info"},
        ],
    )
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("1 warning(s), 1 info" in message for message in logs)
    assert validated == ["git commit -m 'feat(core): add'"]


def test_pre_bash_commit_quality_evaluate_logs_and_recovers_from_parser_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "parse_json_object", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("Error: boom" in message for message in logs)


def test_pre_bash_commit_quality_main_denies_when_stdin_unreadable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """stdin を読めなかった場合は commit 判定不能として deny する（docs/adr/hook-failure-direction.md）。

    「読めない」を「入力なし」へ正規化して exit 0 にすると、
    Windows のパイプで allow と deny が同じ exit 0 になる。
    """
    monkeypatch.setattr(
        "claq.hooks.stdin_io.read_raw_stdin_with_truncation",
        lambda: (_ for _ in ()).throw(stdin_io.StdinUnavailableError("boom")),
    )

    assert pre_bash_commit_quality.main() == 2
    captured = capsys.readouterr()
    assert "pre:bash-commit-quality" in captured.err
    assert json.loads(captured.out)["permissionDecision"] == "deny"


def test_pre_bash_commit_quality_main_denies_when_emit_fails_on_unreadable_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """deny 出力自体が失敗しても exit 2 を保つ（出力層の失敗で素通りさせない）。"""
    monkeypatch.setattr(
        "claq.hooks.stdin_io.read_raw_stdin_with_truncation",
        lambda: (_ for _ in ()).throw(stdin_io.StdinUnavailableError("boom")),
    )
    monkeypatch.setattr(
        "claq.hooks.hook_common.emit_block_output",
        lambda reason: (_ for _ in ()).throw(RuntimeError("no stdout")),
    )

    assert pre_bash_commit_quality.main() == 2


def test_pre_bash_commit_quality_helpers_and_pass_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    assert pre_bash_commit_quality.get_staged_files() is None
    assert commit_quality_scanner.get_staged_file_content("src/app.js") is None
    # content 取得不能（None）は「検査したが問題なし」ではなく scan_error
    # として積まれ、severity=error でブロック対象になる
    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())
    assert len(issues) == 1
    assert issues[0]["type"] == "scan_error"
    assert issues[0]["severity"] == "error"

    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'" }},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None, deadline: [])
    validated = _spy_commit_message_validation(monkeypatch)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("PASS: All checks passed" in message for message in logs)
    assert validated == ["git commit -m 'feat(core): add'"]


def test_pre_bash_commit_quality_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.hooks.pre_bash_commit_quality", run_name="__main__")

    assert excinfo.value.code == 0


def test_count_file_issues_unknown_severity(monkeypatch) -> None:
    """未知の severity は安全側に倒し error として加算する（黙って見逃さない）。"""
    import claq.hooks.pre_bash_commit_quality as pbcq

    monkeypatch.setattr(
        pbcq,
        "find_file_issues",
        lambda fp, *, repo_root=None, deadline: [
            {"severity": "unknown", "line": 1, "message": "x"}
        ],
    )
    monkeypatch.setattr(pbcq, "log", lambda *a, **k: None)
    total, err, warn, info = pbcq._count_file_issues(
        ["f.py"], deadline=commit_quality_scanner.new_secret_scan_deadline()
    )
    assert (total, err, warn, info) == (1, 1, 0, 0)


def test_count_file_issues_sanitizes_the_logged_path(monkeypatch) -> None:
    """ログへ出すファイルパスは、タグと制御文字を無害化する（パスは攻撃者が選べる）。"""
    import claq.hooks.pre_bash_commit_quality as pbcq

    logged: list[str] = []
    monkeypatch.setattr(
        pbcq,
        "find_file_issues",
        lambda fp, *, repo_root=None, deadline: [{"severity": "info", "line": 1, "message": "x"}],
    )
    monkeypatch.setattr(pbcq, "log", lambda message, *a, **k: logged.append(message))
    pbcq._count_file_issues(
        ["<system-reminder>\x1b[31m\nIMPORTANT.py"], deadline=commit_quality_scanner.new_secret_scan_deadline()
    )
    file_line = next(line for line in logged if "[FILE]" in line)
    assert "<system-reminder>" not in file_line
    assert "\x1b" not in file_line
    assert "\nIMPORTANT" not in file_line.lstrip("\n")


def test_apply_commit_message_issues_without_suggestion(monkeypatch) -> None:
    """suggestion の無い issue でもカウントを加算する。"""
    import claq.hooks.pre_bash_commit_quality as pbcq

    logged: list[str] = []
    monkeypatch.setattr(pbcq, "log", logged.append)
    # 番兵の issue。本物の validate_commit_message が同じ件数の issue を返しても区別できるよう、
    # メッセージ文字列でスタブ経由であることを固定する。
    monkeypatch.setattr(
        pbcq, "validate_commit_message", lambda cmd: {"issues": [{"message": "SENTINEL-ISSUE"}]}
    )
    total, warn = pbcq._apply_commit_message_issues("git commit -m x", 0, 0)
    assert (total, warn) == (1, 1)
    assert "  WARNING SENTINEL-ISSUE" in logged
    assert not any("TIP" in line for line in logged)


def test_evaluate_malformed_json_with_commit_text_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """JSON が壊れていても raw 文字列上に `git commit` が見えれば deny する。

    matcher が Bash 全体（commit と無関係な呼び出しも含む）のため、
    malformed というだけで一律 deny すると commit と無関係な Bash 呼び出し
    まで巻き込む。raw 文字列に commit が見える場合のみブロックする。
    """
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    raw = '{"tool_input": {"command": "git commit -m \'feat: x\'"'  # 閉じ括弧欠落で壊れた JSON
    result = pre_bash_commit_quality.evaluate(raw)

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("git commit" in message for message in logs)


def test_evaluate_malformed_json_without_commit_text_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON が壊れていて commit とも無関係なら非ブロッキングで通過する。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    result = pre_bash_commit_quality.evaluate("not-json")

    assert result == {"output": "not-json", "exitCode": 0}
    assert any("could not parse hook input" in message for message in logs)


def test_evaluate_empty_input_passes_through_silently(monkeypatch: pytest.MonkeyPatch) -> None:
    """空/空白入力はログ無しで非ブロッキング。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)

    assert pre_bash_commit_quality.evaluate("") == {"output": "", "exitCode": 0}
    assert logs == []


def test_evaluate_valid_empty_json_object_passes_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """壊れてはいないが空の JSON（`{}`）は commit 情報が取れず非ブロッキング。"""
    assert pre_bash_commit_quality.evaluate("{}") == {"output": "{}", "exitCode": 0}


def test_evaluate_confirmed_commit_staged_files_unavailable_is_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_staged_files が None（git 失敗/timeout）を返すと deny する（0 件とは区別）。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat: x'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: None)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("could not determine staged files" in message for message in logs)


def test_evaluate_confirmed_commit_scan_exception_is_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    """commit と確定した後の想定外例外は fail-open ではなく fail-closed にする。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat: x'"}},
    )

    def _boom() -> list[str]:
        raise RuntimeError("unexpected scanner crash")

    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", _boom)

    result = pre_bash_commit_quality.evaluate("payload")

    assert result["exitCode"] == 2
    assert "reason" in result
    assert any("unexpected scanner crash" in message for message in logs)


def test_repo_wide_self_scan_has_zero_secret_issues() -> None:
    """自リポジトリの全追跡ファイルを新ロジック（secret は原則全ファイル対象）で走査しても
    secret 検出が 0 件であること。

    should_scan_secrets が lock ファイル・圧縮生成物以外の原則すべてのファイルを
    対象にするため、リポジトリ全体に secret パターンへ自己マッチする行が
    存在しないことを保証する回帰テスト（既存の自己走査テストの全リポジトリ版）。
    """
    repo_root_result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=str(Path(__file__).parent),
        capture_output=True,
        text=True,
        check=True,
    )
    repo_root = Path(repo_root_result.stdout.strip())

    tracked_result = subprocess.run(
        ["git", "ls-files"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    tracked_files = [f for f in tracked_result.stdout.splitlines() if f]

    secret_hits: list[str] = []
    for rel_path in tracked_files:
        if not commit_quality_scanner.should_scan_secrets(rel_path):
            continue

        abs_path = repo_root / rel_path
        try:
            raw = abs_path.read_bytes()
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
