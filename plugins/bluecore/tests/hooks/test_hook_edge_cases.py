"""追加の hook 分岐と境界値を検証するテスト。"""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bluecore.hooks import commit_quality_scanner as commit_quality_scanner
from bluecore.hooks import pre_bash_commit_quality as pre_bash_commit_quality
from bluecore.hooks import session_start as session_start


def test_session_start_run_injects_checkpoint_and_project_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    learned_dir = tmp_path / "learned"
    sessions_dir = tmp_path / "sessions"
    learned_dir.mkdir()
    sessions_dir.mkdir()

    checkpoint = sessions_dir / "checkpoint-2026-01-01-repo.md"
    checkpoint.write_text("---\ncompleted: false\n---\n\x1b[31m進行中の作業\x1b[0m", encoding="utf-8")

    logs: list[str] = []

    def fake_find_files(dir_path: Path, pattern: str, max_age: int = 7) -> list[dict[str, object]]:
        if pattern == "checkpoint-*.md" and dir_path == sessions_dir:
            return [{"path": str(checkpoint), "mtime": 200.0}]
        if pattern == "*.md" and dir_path == learned_dir:
            return [{"path": str(learned_dir / "skill.md"), "mtime": 1.0}]
        return []

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: learned_dir)
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_start, "find_files", fake_find_files)
    monkeypatch.setattr(session_start, "read_file", lambda path: checkpoint.read_text(encoding="utf-8"))
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name="npm", source="auto"))
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=["python"], frameworks=["pytest"], primary_language="python"),
    )
    monkeypatch.setattr(session_start, "log", logs.append)

    payload = json.loads(session_start.run(""))
    additional_context = payload["hookSpecificOutput"]["additionalContext"]

    assert "Active checkpoint:" in additional_context
    assert "進行中の作業" in additional_context
    assert "\x1b[" not in additional_context
    assert "Project type:" in additional_context
    assert "Previous session summary:" not in additional_context
    assert any("learned skill(s) available" in message for message in logs)
    assert any("Package manager: npm" in message for message in logs)


def test_session_start_run_emits_empty_context_and_prompts_for_pm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs: list[str] = []
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(session_start, "find_files", lambda dir_path, pattern, max_age=7: [])
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source="auto"))
    monkeypatch.setattr(session_start, "get_selection_prompt", lambda: "SELECT A PACKAGE MANAGER")
    monkeypatch.setattr(session_start.Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=[], frameworks=[], primary_language=None),
    )
    monkeypatch.setattr(session_start, "log", logs.append)

    payload = json.loads(session_start.run(""))

    assert payload["hookSpecificOutput"]["additionalContext"] == ""
    assert any("SELECT A PACKAGE MANAGER" in message for message in logs)
    assert any("No specific project type detected" in message for message in logs)


def test_session_start_main_logs_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(session_start, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(session_start, "log", logs.append)

    assert session_start.main() == 0
    assert any("Error: boom" in message for message in logs)


def test_pre_bash_commit_quality_detects_file_issues_and_commit_message_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "\n".join(
        [
            'console.log("hi")',  # nosec
            "// console.log('commented')",  # nosec
            "debugger",  # nosec
            "// TODO: clean this up",  # nosec
            "// TODO: #123 tracked",
            "const " + "api" + "_key" + ' = "abc";',
        ]
    )
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert {issue["type"] for issue in issues} == {"console.log", "debugger", "todo", "secret"}  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "console.log"] == [1]  # nosec
    assert [issue["line"] for issue in issues if issue["type"] == "todo"] == [4]

    assert pre_bash_commit_quality.validate_commit_message("git status") is None
    message = pre_bash_commit_quality.validate_commit_message('git commit -m "feat(core): Add feature."')
    assert message is not None
    assert message["message"] == "feat(core): Add feature."
    assert {issue["type"] for issue in message["issues"]} == {"capitalization", "punctuation"}


def test_find_file_issues_skips_nosec_marked_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """# nosec は console.log/debugger/todo を抑制するが、secret 検出は抑制しない。"""
    content = "\n".join(
        [
            'debugger  # nosec',
            "const " + "api" + "_key" + ' = "x";  # nosec',
        ]
    )
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py")

    # デバッガ文は nosec で抑制される
    assert not any(issue["type"] == "debugger" for issue in issues)  # nosec
    # secret は nosec があっても検出される（バイパス防止）
    assert [issue["type"] for issue in issues] == ["secret"]
    assert issues[0]["line"] == 2


def test_find_file_issues_secret_detection_not_bypassed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """`# nosec` を付与しても api_key のようなシークレットパターンはブロック対象として検出され続ける。"""
    content = "api" + "_key" + ' = "hunter2secret"  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.py")

    assert len(issues) == 1
    assert issues[0]["type"] == "secret"
    assert issues[0]["severity"] == "error"


def test_find_file_issues_console_log_still_suppressed_by_nosec(monkeypatch: pytest.MonkeyPatch) -> None:
    """secret を含まない行では従来どおり console.log が nosec で抑制されること。"""  # nosec
    content = 'console.log("debug")  # nosec'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("src/app.py") == []


def test_find_file_issues_self_check_has_zero_secret_issues(monkeypatch: pytest.MonkeyPatch) -> None:
    """このフック自身のソースを検査しても secret 検出が0件であること（自己検出回避の nosec が secret を隠していないことの担保）。"""
    source_path = Path(commit_quality_scanner.__file__)
    own_source = source_path.read_text(encoding="utf-8")

    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: own_source)
    issues = commit_quality_scanner.find_file_issues(str(source_path))

    secret_issues = [issue for issue in issues if issue["type"] == "secret"]
    assert secret_issues == []


def test_find_file_issues_self_check_on_this_test_file_has_zero_issues() -> None:
    """このテストファイル自身を検査しても issue が0件であること（fixture 文字列の自己マッチ回帰防止）。

    cbabdad で secret 検出が nosec 無視・常時実行になった結果、本ファイルの
    シークレットパターン用 fixture 文字列がソース行として secret パターンに
    自己マッチし、本ファイルをコミットすると exitCode=2 でブロックされる
    回帰が発生した。本テストはその回帰を検知する。
    """
    this_file = Path(__file__)
    own_source = this_file.read_text(encoding="utf-8")

    with mock.patch.object(commit_quality_scanner, "get_staged_file_content", return_value=own_source):
        issues = commit_quality_scanner.find_file_issues(str(this_file))

    assert issues == []


def test_find_file_issues_detects_secret_in_non_lint_extension(monkeypatch: pytest.MonkeyPatch) -> None:
    """.py/.js 等の lint 対象外拡張子（例: .env）でも secret 検出が行われること。"""
    content = "API_" + "KEY" + '="hunter2secret"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues(".env")

    assert [issue["type"] for issue in issues] == ["secret"]


def test_find_file_issues_lint_only_checks_skipped_for_non_lint_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """lint 対象外拡張子では console.log/デバッガ文/todo は検出されないこと（secret のみ対象）。"""  # nosec
    content = 'console.log("hi")'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues(".env") == []


def test_find_file_issues_skips_secret_scan_for_lock_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """ロックファイルは内容が secret パターンに一致しても検出しないこと。"""
    content = "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    assert commit_quality_scanner.find_file_issues("package-lock.json") == []


def test_find_file_issues_oversized_files_truncate_secret_scan_not_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1MB を超えるファイルは secret スキャンを全面放棄せず、先頭
    _SECRET_SCAN_MAX_BYTES バイトに切り詰めて継続すること（水増しによる
    全面回避の防止）。境界を超えた末尾側の secret はこの実装では検出でき
    ない（切り詰めの仕様上の限界）ため、lint（ログ出力チェック）は継続
    検出されることも合わせて確認する。"""  # nosec
    padding = "x" * (1024 * 1024 + 1)
    content = padding + "\n" + 'console.log("hi")' + "\n" + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    types = {issue["type"] for issue in issues}
    # 切り詰め境界より後ろにある secret は検出できない（仕様上の限界）
    assert "secret" not in types
    # lint はサイズに関わらず継続する
    assert "console.log" in types  # nosec


def test_find_file_issues_oversized_files_scan_prefix_for_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """1MB 超のファイルでも、切り詰め境界より前（先頭側）にある secret は
    検出されること（全面スキップではなく先頭側スキャン継続の確認）。"""
    secret_line = "api" + "_key" + ' = "abc123"'
    padding = "x" * (1024 * 1024 + 1)
    content = secret_line + "\n" + padding
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert any(issue["type"] == "secret" and issue["line"] == 1 for issue in issues)


def test_find_file_issues_secret_scan_applies_under_size_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """1MB 以下のファイルは通常どおり secret スキャンされること（境界値確認）。"""
    padding = "x" * (1024 * 1024 - 100)
    content = padding + "\n" + "api" + "_key" + ' = "abc123"'
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("src/app.js")

    assert any(issue["type"] == "secret" for issue in issues)


def test_find_file_issues_binary_file_skips_lint_but_still_scans_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """バイナリ判定（先頭に NUL を含む）は lint 抑制のみに用い、secret 検出は
    継続すること（NUL バイトを1つ混ぜるだけで secret 検査を回避できてしまう
    抜け道の回帰防止）。"""
    content = "\0binary preamble\n" + 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("weird.js")

    types = {issue["type"] for issue in issues}
    assert "secret" in types
    assert "console.log" not in types  # nosec


def test_evaluate_scans_non_lint_extension_files_for_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    """evaluate() が lint 非対象拡張子（.env 等）もステージ済みファイルの走査対象に含めること。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: [".env", "package-lock.json"])

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    # .env は secret スキャン対象なので走査される。package-lock.json は
    # lint 非対象・secret 対象外の双方であるため走査対象から除外される。
    assert seen == [".env"]


def test_pre_bash_commit_quality_helpers_handle_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )

    assert pre_bash_commit_quality.get_staged_files() == []
    assert commit_quality_scanner.get_staged_file_content("src/app.js") is None
    assert commit_quality_scanner.should_lint_file("src/app.py")
    assert not commit_quality_scanner.should_lint_file("src/app.txt")


@pytest.mark.parametrize(
    "file_path",
    [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Cargo.lock",
        "poetry.lock",
        "uv.lock",
        "Pipfile.lock",
        "vendor/package-lock.json",
        "dist/bundle.min.js",
        "static/app.min.css",
    ],
)
def test_should_scan_secrets_excludes_lock_and_minified_files(file_path: str) -> None:
    """ロックファイルと圧縮生成物（*.min.js/*.min.css）は secret スキャン対象外であること。"""
    assert pre_bash_commit_quality.should_scan_secrets(file_path) is False


@pytest.mark.parametrize(
    "file_path",
    [
        ".env",
        "config.json",
        "app.yaml",
        "deploy.sh",
        "Dockerfile",
        "src/app.txt",
        "tests/fixtures/secret.env",
    ],
)
def test_should_scan_secrets_includes_non_lint_extensions(file_path: str) -> None:
    """.env/.yaml/.sh/Dockerfile 等、lint 対象外の拡張子でも secret スキャン対象であること
    （tests/ 配下も除外しない）。"""
    assert pre_bash_commit_quality.should_scan_secrets(file_path) is True


def test_pre_bash_commit_quality_helpers_return_success_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], *, capture_output: bool, check: bool, text: bool = False):
        if command[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/app.py\nsrc/tool.ts\n", stderr="")
        if command[:2] == ["git", "show"]:
            # get_staged_file_content はバイナリ判定のため text=True を付けずに bytes で取得する
            return subprocess.CompletedProcess(command, 0, stdout=b"file content", stderr=b"")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    monkeypatch.setattr(commit_quality_scanner.subprocess, "run", fake_run)

    assert pre_bash_commit_quality.get_staged_files() == ["src/app.py", "src/tool.ts"]
    assert commit_quality_scanner.get_staged_file_content("src/app.js") == "file content"


def test_get_staged_file_content_does_not_filter_binary_content(monkeypatch: pytest.MonkeyPatch) -> None:
    """get_staged_file_content 自体はバイナリ判定を行わず、NUL を含む内容もそのまま返すこと。

    バイナリ判定（`_is_binary_content`）は `find_file_issues` 側で lint 抑制の
    みに使う設計であり、ここで None を返してしまうと secret 検出まで巻き
    込んで全放棄されてしまう（NUL 1個で secret 検査を回避できる抜け道）ため、
    取得層では判定しないことを保証する。
    """
    binary_bytes = b"PNG\x00\x01\x02fake image bytes"

    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=binary_bytes, stderr=b""),
    )
    content = commit_quality_scanner.get_staged_file_content("image.png")
    assert content is not None
    assert "\0" in content


def test_get_staged_file_content_replaces_invalid_utf8_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """不正な UTF-8 バイト列でも UnicodeDecodeError を送出せず置換して返すこと
    （旧 text=True 実装の潜在バグの回帰防止）。"""
    invalid_utf8 = b"valid text \xff\xfe invalid bytes"

    monkeypatch.setattr(
        commit_quality_scanner.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout=invalid_utf8, stderr=b""),
    )
    content = commit_quality_scanner.get_staged_file_content("weird_encoding.txt")
    assert content is not None
    assert "valid text" in content


def test_pre_bash_commit_quality_finds_parser_and_reading_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    assert commit_quality_scanner.find_file_issues("src/app.js") == []

    long_message = "git commit -m \"bad message with no conventional format and a very long subject line that keeps going.\""
    parsed = pre_bash_commit_quality.validate_commit_message(long_message)
    assert parsed is not None
    assert {issue["type"] for issue in parsed["issues"]} >= {"format", "length"}


def test_pre_bash_commit_quality_run_wrapper_and_main_success(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pre_bash_commit_quality.run("payload") == pre_bash_commit_quality.evaluate("payload")

    monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "payload")
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
        lambda raw: {"tool_input": {"command": "git commit --amend -m 'feat(core): add'"}},
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
        lambda path, *, repo_root=None: [{"severity": "error", "line": 1, "message": "boom"}],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)
    assert pre_bash_commit_quality.evaluate("payload") == {
        "output": "payload",
        "exitCode": 2,
        "reason": "[Hook] BLOCKED: 1 error(s) found in staged files. Fix them before committing.",
    }

    warning_logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", warning_logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
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
        lambda path, *, repo_root=None: [
            {"severity": "warning", "line": 1, "message": "warn"},
            {"severity": "info", "line": 2, "message": "info"},
        ],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("1 warning(s), 1 info" in message for message in logs)


def test_pre_bash_commit_quality_evaluate_logs_and_recovers_from_parser_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "parse_json_object", lambda raw: (_ for _ in ()).throw(RuntimeError("boom")))

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("Error: boom" in message for message in logs)


def test_pre_bash_commit_quality_main_handles_reader_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "bluecore.hooks.hook_common.read_raw_stdin",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert pre_bash_commit_quality.main() == 0


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

    assert pre_bash_commit_quality.get_staged_files() == []
    assert commit_quality_scanner.get_staged_file_content("src/app.js") is None
    assert commit_quality_scanner.find_file_issues("src/app.js") == []

    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'" }},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_lint_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("PASS: All checks passed" in message for message in logs)


def test_pre_bash_commit_quality_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.pre_bash_commit_quality", run_name="__main__")

    assert excinfo.value.code == 0


def test_validate_commit_message_lowercase_no_period() -> None:
    """conventional commit で小文字始まり・末尾ピリオド無しなら指摘なし。"""
    from bluecore.hooks.pre_bash_commit_quality import validate_commit_message

    result = validate_commit_message('git commit -m "feat: add new feature"')
    assert result is not None
    types = {i["type"] for i in result["issues"]}
    assert "capitalization" not in types
    assert "punctuation" not in types


def test_count_file_issues_unknown_severity(monkeypatch) -> None:
    """未知の severity は error/warning/info いずれにも加算しない。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    monkeypatch.setattr(
        pbcq, "find_file_issues", lambda fp, *, repo_root=None: [{"severity": "unknown", "line": 1, "message": "x"}]
    )
    monkeypatch.setattr(pbcq, "log", lambda *a, **k: None)
    total, err, warn, info = pbcq._count_file_issues(["f.py"])
    assert (total, err, warn, info) == (1, 0, 0, 0)


def test_apply_commit_message_issues_without_suggestion(monkeypatch) -> None:
    """suggestion の無い issue でもカウントを加算する。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    monkeypatch.setattr(pbcq, "log", lambda *a, **k: None)
    monkeypatch.setattr(pbcq, "validate_commit_message", lambda cmd: {"issues": [{"message": "m"}]})
    total, warn = pbcq._apply_commit_message_issues("git commit -m x", 0, 0)
    assert (total, warn) == (1, 1)


# ─────────────────────────────────────────────
# _is_git_commit_command / _is_amend_commit / _is_commit_all_flag（トークン化）
# ─────────────────────────────────────────────


def test_is_git_commit_command_detects_double_space() -> None:
    """連続空白（git  commit）でも commit 検出が成立すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git  commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_detects_newline_separated() -> None:
    """改行区切り（git\\ncommit）でも commit 検出が成立すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, _args = pbcq._is_git_commit_command("git\ncommit -m x")
    assert is_commit is True


def test_is_git_commit_command_skips_global_dash_c_option() -> None:
    """`git -C <path> commit` のようなグローバルオプション付きでも検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git -C /tmp/repo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_with_value() -> None:
    """allowlist に無いグローバルオプション（--exec-path 等）でも検出漏れしないこと。

    旧実装は既知オプション（-C/-c 等）のみ値トークンをスキップしていたため、
    `--exec-path <path>` のような未知オプションの値トークンで走査が
    打ち切られ検出漏れになっていた（フェイルセーフ違反）。新実装は
    オプション・値を区別せず commit まで読み飛ばす。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --exec-path /foo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_detects_dash_a() -> None:
    """未知グローバルオプション経由でも -a 相当のフラグが検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --super-prefix /x commit -am y")
    assert is_commit is True
    assert pbcq._is_commit_all_flag(args) is True


def test_is_git_commit_command_detects_within_composite_command() -> None:
    """`&&` で連結された複合コマンド中の git commit も検出できること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("echo hi && git commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_returns_false_for_non_commit() -> None:
    """git commit を含まないコマンドは False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git status")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_falls_back_on_shlex_failure() -> None:
    """クォート不整合（heredoc 等）で shlex.split が失敗しても検出を継続すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    broken_command = "git commit -m 'unterminated quote"
    is_commit, _args = pbcq._is_git_commit_command(broken_command)
    assert is_commit is True


def test_is_git_commit_command_fallback_non_commit_returns_false() -> None:
    """フォールバック経路でも git commit を含まなければ False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    broken_command = "echo 'unterminated quote"
    is_commit, args = pbcq._is_git_commit_command(broken_command)
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_returns_false_when_only_options_follow_git() -> None:
    """git の後がオプションのみで commit が現れなければ False を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git --version")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_regex_fallback_true_when_tokens_miss_it() -> None:
    """トークン走査では見つからなくても、引用符内のテキスト等で `git commit` が
    文字列として現れれば保守的に True を返すこと（過剰検出側のフェイルセーフ）。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    # 実際は `git status` だが、引用符内のメッセージに "git commit" という
    # 語が偶然含まれるケース。トークン走査では検出できないため、regex
    # フォールバックが保守的に True を返す。
    is_commit, args = pbcq._is_git_commit_command("git status -m 'please run git commit later'")
    assert is_commit is True
    assert args == []


def test_is_amend_commit_detects_token() -> None:
    """--amend トークンが引数中にあれば True を返すこと。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    assert pbcq._is_amend_commit(["--amend", "-m", "x"]) is True
    assert pbcq._is_amend_commit(["-m", "x"]) is False


def test_is_commit_all_flag_detects_short_long_and_combined() -> None:
    """-a / --all / -am のような結合短形式を検出すること。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    assert pbcq._is_commit_all_flag(["-a", "-m", "x"]) is True
    assert pbcq._is_commit_all_flag(["--all", "-m", "x"]) is True
    assert pbcq._is_commit_all_flag(["-am", "x"]) is True
    assert pbcq._is_commit_all_flag(["-m", "x"]) is False
    assert pbcq._is_commit_all_flag(["--amend", "-m", "x"]) is False


def test_evaluate_commit_amend_skipped_with_messy_spacing(monkeypatch: pytest.MonkeyPatch) -> None:
    """連続空白を含む --amend コマンドでも従来どおりスキップされること。"""
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git   commit  --amend -m 'feat(core): add'"}},
    )
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "get_staged_files",
        lambda: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}


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
    monkeypatch.setattr(pre_bash_commit_quality, "_resolve_repo_root", lambda: dummy_repo_root)

    seen: list[tuple[str, Path | None]] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append((path, repo_root))
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert dict(seen) == {"src/staged.py": None, "src/unstaged.py": dummy_repo_root}


def test_evaluate_commit_dash_a_worktree_skipped_when_repo_root_unresolvable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """repo root が解決できない場合、-a の未ステージ分は非ブロッキングでスキップされること。"""
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
    monkeypatch.setattr(pre_bash_commit_quality, "_resolve_repo_root", lambda: None)

    seen: list[str] = []

    def _fake_find_file_issues(path: str, *, repo_root: Path | None = None) -> list[dict]:
        seen.append(path)
        return []

    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", _fake_find_file_issues)
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert seen == ["src/staged.py"]


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
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path, *, repo_root=None: [])
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}


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
    従来の staged 経路の不変性確認）。"""
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

    def fake_run(command: list[str], *, capture_output: bool, text: bool, check: bool):
        assert command == ["git", "diff", "HEAD", "--name-only", "--diff-filter=ACMR"]
        return subprocess.CompletedProcess(command, 0, stdout="src/a.py\nsrc/b.py\n", stderr="")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)
    assert pre_bash_commit_quality.get_unstaged_modified_files() == ["src/a.py", "src/b.py"]


def test_get_unstaged_modified_files_returns_empty_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """HEAD が存在しない等で失敗した場合は空リストを返すこと（非ブロッキング）。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stdout="", stderr="fatal"),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() == []


def test_get_unstaged_modified_files_returns_empty_on_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """subprocess が OSError 系例外を投げても空リストを返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )
    assert pre_bash_commit_quality.get_unstaged_modified_files() == []


def test_resolve_repo_root_returns_none_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse が失敗（returncode != 0）した場合は None を返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stdout="", stderr="fatal"),
    )
    assert pre_bash_commit_quality._resolve_repo_root() is None


def test_resolve_repo_root_returns_none_on_empty_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse の出力が空の場合は None を返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
    )
    assert pre_bash_commit_quality._resolve_repo_root() is None


def test_resolve_repo_root_returns_none_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse がタイムアウトした場合は非ブロッキングで None を返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="git", timeout=5)),
    )
    assert pre_bash_commit_quality._resolve_repo_root() is None


def test_resolve_repo_root_returns_path_on_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """git rev-parse が成功すればそのパスを返すこと。"""
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="/repo/root\n", stderr=""),
    )
    assert pre_bash_commit_quality._resolve_repo_root() == Path("/repo/root")


def test_get_worktree_file_content_returns_none_on_oserror(tmp_path: Path) -> None:
    """作業ツリーにファイルが無い等で読み取れない場合は None を返すこと。"""
    assert commit_quality_scanner.get_worktree_file_content(tmp_path, "missing.py") is None


def test_get_worktree_file_content_reads_real_file(tmp_path: Path) -> None:
    """作業ツリー上の実ファイルを読み取れること。"""
    (tmp_path / "app.py").write_text("value = 1\n", encoding="utf-8")
    content = commit_quality_scanner.get_worktree_file_content(tmp_path, "app.py")
    assert content == "value = 1\n"


def test_get_worktree_file_content_rejects_symlink_escaping_repo_root(tmp_path: Path) -> None:
    """repo 外の実体を指すシンボリックリンクは None を返すこと（realpath 包含チェック）。

    OS レベルでシンボリックリンクを追跡すると、repo 内の悪意あるリンクが
    `git commit -a` の対象に入った場合に repo 外の実体（秘密鍵等）を読み、
    secret パターン一致でログに一部露出しうる欠陥の回帰防止。
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("api" + "_key" + ' = "hunter2secret"', encoding="utf-8")

    link = repo_root / "evil_link.py"
    link.symlink_to(outside_secret)

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "evil_link.py") is None


def test_get_worktree_file_content_rejects_absolute_file_path(tmp_path: Path) -> None:
    """file_path が絶対パスの場合は None を返すこと（pathlib の仕様上 repo_root が無視される経路）。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret content", encoding="utf-8")

    assert commit_quality_scanner.get_worktree_file_content(repo_root, str(outside_secret)) is None


def test_get_worktree_file_content_rejects_dotdot_traversal(tmp_path: Path) -> None:
    """`..` を含む file_path で repo_root 外へ脱出しようとした場合は None を返すこと。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret content", encoding="utf-8")

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "../outside_secret.txt") is None


def test_get_worktree_file_content_rejects_symlinked_intermediate_directory(tmp_path: Path) -> None:
    """中間ディレクトリがシンボリックリンクで repo 外へ脱出する場合も None を返すこと。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "app.py").write_text("secret content", encoding="utf-8")

    linked_subdir = repo_root / "linked_subdir"
    linked_subdir.symlink_to(outside_dir)

    assert commit_quality_scanner.get_worktree_file_content(repo_root, "linked_subdir/app.py") is None


def test_find_git_commit_args_stops_at_separator_before_commit() -> None:
    """git の直後にシェル区切りが現れた場合、その git 呼び出しは commit と
    みなさないこと（オプション読み飛ばしのループが区切りで打ち切られる分岐）。
    """
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git && echo hi")
    assert is_commit is False
    assert args == []


def test_collect_args_until_separator_stops_at_shell_operator() -> None:
    """`git commit` 後の引数収集は `&&` / `;` / `|` で打ち切る。"""
    import bluecore.hooks.pre_bash_commit_quality as pbcq

    is_commit, args = pbcq._is_git_commit_command("git commit -m x && echo hi")
    assert is_commit is True
    assert args == ["-m", "x"]
    assert pbcq._collect_args_until_separator(["-m", "x", ";", "true"], 0) == ["-m", "x"]
    assert pbcq._collect_args_until_separator(["&&", "echo"], 0) == []


def test_iter_assistant_tool_uses_non_list_content() -> None:
    """assistant の message.content が list でなければ空リストを返す。"""
    from bluecore.hooks import session_end

    assert session_end._iter_assistant_tool_uses({"type": "assistant", "message": {"content": "plain text"}}) == []
    assert session_end._iter_assistant_tool_uses({"type": "assistant", "message": {}}) == []
    assert session_end._iter_assistant_tool_uses({"type": "assistant"}) == []


def test_find_file_issues_minified_js_lints_but_skips_secret_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """圧縮生成物（*.min.js）は lint 対象（拡張子は .js）だが secret スキャンは
    対象外であること（do_lint=True かつ do_secrets=False の組み合わせ）。"""
    content = 'console.log("hi")\n' + "api" + "_key" + ' = "abc123"'  # nosec
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)

    issues = commit_quality_scanner.find_file_issues("dist/app.min.js")

    types = {issue["type"] for issue in issues}
    assert "console.log" in types  # nosec
    assert "secret" not in types


def test_repo_wide_self_scan_has_zero_secret_issues() -> None:
    """自リポジトリの全追跡ファイルを新ロジック（secret は原則全ファイル対象）で走査しても
    secret 検出が0件であること。

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
            issues = commit_quality_scanner.find_file_issues(rel_path)

        secret_hits.extend(f"{rel_path}:{issue['line']}" for issue in issues if issue["type"] == "secret")

    assert secret_hits == []
