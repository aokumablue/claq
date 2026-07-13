"""追加の hook 分岐と境界値を検証するテスト。"""

from __future__ import annotations

import importlib
import io
import json
import os
import runpy
import subprocess
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from bluecore.hooks import commit_quality_scanner as commit_quality_scanner
from bluecore.hooks import insights_security_monitor as insights_security_monitor
from bluecore.hooks import pre_bash_commit_quality as pre_bash_commit_quality
from bluecore.hooks import run_with_flags as run_with_flags
from bluecore.hooks import session_start as session_start


def test_run_with_flags_reports_error_when_not_enough_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_stderr: list[str] = []
    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "hook-only"])
    monkeypatch.setattr(run_with_flags, "write_stderr", captured_stderr.append)

    assert run_with_flags.main() == 1
    assert any("引数が不足" in msg for msg in captured_stderr)


def test_run_with_flags_builds_env_and_emits_no_stdout_on_empty_child_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout: list[str] = []
    stderr: list[str] = []
    captured: dict[str, object] = {}

    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "hook-id", "target", "standard", "alpha"])
    monkeypatch.setattr(run_with_flags, "read_raw_stdin_with_truncation", lambda max_bytes=0: ("payload", True))
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: True)

    def fake_run(
        command: list[str],
        *,
        input: str,
        text: bool,
        capture_output: bool,
        env: dict[str, str],
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = input
        captured["env"] = env
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="child stderr")

    monkeypatch.setattr(run_with_flags.subprocess, "run", fake_run)
    monkeypatch.setattr(run_with_flags, "write_stdout", stdout.append)
    monkeypatch.setattr(run_with_flags, "write_stderr", stderr.append)

    assert run_with_flags.main() == 0
    assert stdout == []
    assert stderr == ["child stderr"]
    assert captured["input"] == "payload"
    assert captured["command"] == [sys.executable, "-m", "target", "alpha"]
    env = captured["env"]
    assert isinstance(env, dict)
    assert "BLUECORE_HOOK_INPUT_TRUNCATED" not in env
    assert "BLUECORE_HOOK_INPUT_MAX_BYTES" not in env
    assert env["PYTHONPATH"].startswith(str(run_with_flags.REPO_ROOT / "src"))


def test_run_with_flags_reports_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    stderr: list[str] = []

    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "hook-id", "target"])
    monkeypatch.setattr(run_with_flags, "read_raw_stdin_with_truncation", lambda max_bytes=0: ("payload", False))
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: True)
    monkeypatch.setattr(run_with_flags.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    monkeypatch.setattr(run_with_flags, "write_stderr", stderr.append)

    assert run_with_flags.main() == 1
    assert any("Error running hook-id" in message for message in stderr)


def test_run_with_flags_reads_and_truncates_utf8_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyStdin:
        def __init__(self, raw: bytes) -> None:
            self.buffer = io.BytesIO(raw)

    monkeypatch.setattr(run_with_flags.sys, "stdin", DummyStdin(b"\xe3\x81\x82"))

    text, truncated = run_with_flags.read_raw_stdin_with_truncation(2)

    assert truncated is True
    assert text == "�"


def test_run_with_flags_reads_without_stdin_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    class DummyTextStdin:
        def __init__(self, raw: str) -> None:
            self._stream = io.StringIO(raw)
            self.read_calls: list[int] = []

        def read(self, size: int = -1) -> str:
            self.read_calls.append(size)
            return self._stream.read(size)

    dummy_stdin = DummyTextStdin("abcdef")
    monkeypatch.setattr(run_with_flags.sys, "stdin", dummy_stdin)

    text, truncated = run_with_flags.read_raw_stdin_with_truncation(3)

    assert truncated is True
    assert text == "abc"
    assert dummy_stdin.read_calls == [4]


def test_run_with_flags_returns_child_stdout_when_hook_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout: list[str] = []
    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "hook-id", "target"])
    monkeypatch.setattr(run_with_flags, "read_raw_stdin_with_truncation", lambda max_bytes=0: ("payload", False))
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: True)
    monkeypatch.setattr(
        run_with_flags.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="child-out", stderr=""),
    )
    monkeypatch.setattr(run_with_flags, "write_stdout", stdout.append)

    assert run_with_flags.main() == 0
    assert stdout == ["child-out"]


def test_run_with_flags_emits_session_start_fallback_json_when_child_stdout_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout: list[str] = []
    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "session:start", "target"])
    monkeypatch.setattr(run_with_flags, "read_raw_stdin_with_truncation", lambda max_bytes=0: ("payload", False))
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: True)
    monkeypatch.setattr(
        run_with_flags.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
    )
    monkeypatch.setattr(run_with_flags, "write_stdout", stdout.append)

    assert run_with_flags.main() == 0
    payload = json.loads(stdout[0])
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == ""


def test_run_with_flags_skips_disabled_hook_with_unbounded_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = "payload-" + "x" * 1024
    calls: list[int] = []

    class _Buffer:
        def read(self) -> bytes:
            calls.append(-1)
            return payload.encode("utf-8")

    class _StdIn:
        def __init__(self) -> None:
            self.buffer = _Buffer()

        def read(self, *_args: object, **_kwargs: object) -> str:
            raise AssertionError("disabled hook path must not use truncated stdin reader")

    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "hook-id", "target"])
    monkeypatch.setattr(run_with_flags.sys, "stdin", _StdIn())
    monkeypatch.setattr(
        run_with_flags,
        "read_raw_stdin_with_truncation",
        lambda max_bytes=0: (_ for _ in ()).throw(AssertionError("should not be called")),
    )
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: False)
    monkeypatch.setattr(
        run_with_flags.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run")),
    )

    assert run_with_flags.main() == 0
    # hook 無効時は stdin を読み捨てるだけで stdout には何も出さない
    assert calls == [-1]


def test_run_with_flags_blocks_truncated_payload_for_guarded_hook(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout: list[str] = []
    stderr: list[str] = []

    monkeypatch.setattr(run_with_flags.sys, "argv", ["launcher.py", "pre:config-protection", "target-module"])
    monkeypatch.setattr(run_with_flags, "read_raw_stdin_with_truncation", lambda max_bytes=0: ("payload", True))
    monkeypatch.setattr(run_with_flags, "is_hook_enabled", lambda hook_id, profiles=None: True)
    monkeypatch.setattr(run_with_flags, "write_stdout", stdout.append)
    monkeypatch.setattr(run_with_flags, "write_stderr", stderr.append)

    assert run_with_flags.main() == 2
    assert stdout == []
    assert any("BLOCKED: Hook input exceeded" in message for message in stderr)


def test_run_with_flags_resolve_target_command_accepts_executable_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    relative = plugin_root / "tool"
    relative.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    relative.chmod(0o755)

    monkeypatch.setattr(run_with_flags, "REPO_ROOT", plugin_root)

    assert run_with_flags.resolve_target_command("tool", ["x"]) == [str(relative), "x"]

    absolute = tmp_path / "absolute-tool"
    absolute.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    absolute.chmod(0o755)

    assert run_with_flags.resolve_target_command(str(absolute)) == [str(absolute)]


def test_session_start_run_injects_previous_session_and_project_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    learned_dir = tmp_path / "learned"
    sessions_dir = tmp_path / "sessions"
    first_dir.mkdir()
    second_dir.mkdir()
    learned_dir.mkdir()
    sessions_dir.mkdir()

    older = first_dir / "daily-session.tmp"
    newer = second_dir / "daily-session.tmp"
    older.write_text("older", encoding="utf-8")
    newer.write_text("\x1b[31mLatest summary\x1b[0m", encoding="utf-8")

    logs: list[str] = []

    def fake_find_files(dir_path: Path, pattern: str, max_age: int = 7) -> list[dict[str, object]]:
        if pattern == "*-session.tmp":
            if dir_path == first_dir:
                return [{"path": str(older), "mtime": 100.0}]
            if dir_path == second_dir:
                return [{"path": str(newer), "mtime": 200.0}]
        if pattern == "*.md" and dir_path == learned_dir:
            return [{"path": str(learned_dir / "skill.md"), "mtime": 1.0}]
        return []

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: learned_dir)
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [first_dir, second_dir])
    monkeypatch.setattr(session_start, "find_files", fake_find_files)
    monkeypatch.setattr(session_start, "read_file", lambda path: newer.read_text(encoding="utf-8"))
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name="npm", source="auto"))
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=["python"], frameworks=["pytest"], primary_language="python"),
    )
    monkeypatch.setattr(session_start, "log", logs.append)

    payload = json.loads(session_start.run(""))
    additional_context = payload["hookSpecificOutput"]["additionalContext"]

    assert "Previous session summary:" in additional_context
    assert "Latest summary" in additional_context
    assert "\x1b[" not in additional_context
    assert "Project type:" in additional_context
    assert any("learned skill(s) available" in message for message in logs)
    assert any("Package manager: npm" in message for message in logs)


def test_session_start_git_info_and_scope_hint_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(session_start, "log", logs.append)

    def fake_check_output(cmd: list[str], **_kwargs: object) -> str:
        if "--is-inside-work-tree" in cmd:
            return "true"
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=b"no branch")
        if cmd[:3] == ["git", "rev-parse", "--short=12"]:
            raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=b"no commit")
        if cmd[:2] == ["git", "status"]:
            return " M file1\n?? file2\n"
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(session_start, "check_output_text", fake_check_output)

    info = session_start._get_git_info()
    assert info["branch"] is None
    assert info["commit_hash"] is None
    assert info["uncommitted_count"] == 2
    assert any("git branch lookup failed" in message for message in logs)
    assert any("git commit lookup failed" in message for message in logs)

    logs.clear()

    def fake_check_output_status(cmd: list[str], **_kwargs: object) -> str:
        if "--is-inside-work-tree" in cmd:
            return "true"
        if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
            return "main\n"
        if cmd[:3] == ["git", "rev-parse", "--short=12"]:
            return "abc123\n"
        if cmd[:2] == ["git", "status"]:
            raise subprocess.CalledProcessError(1, cmd, output=b"", stderr=b"no status")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(session_start, "check_output_text", fake_check_output_status)

    info = session_start._get_git_info()
    assert info["branch"] == "main"
    assert info["commit_hash"] == "abc123"
    assert info["uncommitted_count"] == 0
    assert any("git status lookup failed" in message for message in logs)

    assert session_start._compute_scope_hint(["python"], ["Django"]) == "project"
    assert session_start._compute_scope_hint(["bash", "Shell"], []) == "global"


def test_session_start_run_skips_template_session_and_prompts_for_pm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    logs: list[str] = []
    session_file = tmp_path / "daily-session.tmp"
    session_file.write_text("[Session context goes here]", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: tmp_path / "sessions")
    monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [tmp_path])
    monkeypatch.setattr(
        session_start,
        "find_files",
        lambda dir_path, pattern, max_age=7: [{"path": str(session_file), "mtime": 1.0}] if pattern == "*-session.tmp" else [],
    )
    monkeypatch.setattr(session_start, "read_file", lambda path: session_file.read_text(encoding="utf-8"))
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source="auto"))
    monkeypatch.setattr(session_start, "get_selection_prompt", lambda: "SELECT A PACKAGE MANAGER")
    monkeypatch.setattr(session_start.Path, "cwd", lambda: tmp_path)
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=[], frameworks=[], primary_language=None),
    )
    monkeypatch.setattr(session_start, "log", logs.append)
    monkeypatch.setattr(session_start, "inject_slim_skill", lambda: [])

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


def test_session_start_slim_injection_uses_skill_content(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    learned_dir = tmp_path / "learned"
    sessions_dir = tmp_path / "sessions"
    learned_dir.mkdir()
    sessions_dir.mkdir()

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: learned_dir)
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [])
    monkeypatch.setattr(session_start, "find_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source="auto"))
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=[], frameworks=[], primary_language=None),
    )
    monkeypatch.setattr(session_start, "inject_slim_skill", lambda: ["slim-content"])

    payload = json.loads(session_start.run("{not-json"))
    assert "slim-content" in payload["hookSpecificOutput"]["additionalContext"]


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
    monkeypatch.setattr(pre_bash_commit_quality, "evaluate", lambda raw: {"output": raw, "exitCode": 2})
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert pre_bash_commit_quality.main() == 2

    assert stdout.getvalue() == ""


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
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 2}

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


def test_insights_security_monitor_helpers_and_audit_errors(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    text, context = insights_security_monitor.extract_content(
        {"tool_name": "Write", "tool_input": {"content": "hello world", "file_path": "src/app.py"}}
    )
    assert text == "hello world"
    assert context == "file:src/app.py"

    text, context = insights_security_monitor.extract_content(
        {"tool_name": "Edit", "tool_input": {"new_string": "updated", "file_path": "src/app.py"}}
    )
    assert text == "updated"

    text, context = insights_security_monitor.extract_content(
        {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": "src/app.py",
                "edits": [{"new_string": "first"}, {"new_string": "second"}, "ignored"],
            },
        }
    )
    assert text == "first\nsecond"
    assert context == "file:src/app.py"

    text, context = insights_security_monitor.extract_content(
        {"tool_name": "Bash", "tool_input": {"command": "echo hello"}}
    )
    assert text == "echo hello"
    assert context == "bash:echo hello"

    text, context = insights_security_monitor.extract_content(
        {"content": [{"type": "text", "text": "alpha"}, {"type": "tool", "text": "skip"}], "task": "scan"}
    )
    assert text == "alpha"
    assert context == "scan"

    feedback = insights_security_monitor.format_feedback(
        [SimpleNamespace(severity="CRITICAL", type="LEAK", details="x" * 200)]
    )
    assert "1. [CRITICAL] LEAK" in feedback
    assert "x" * 120 in feedback
    assert "x" * 121 not in feedback

    warnings: list[str] = []
    monkeypatch.setattr(
        insights_security_monitor,
        "log",
        SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg), debug=lambda *a, **k: None),
    )
    monkeypatch.setattr(insights_security_monitor, "AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(insights_security_monitor.os, "open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    insights_security_monitor.write_audit({"tool": "Write"})
    assert any("Failed to write audit log" in message for message in warnings)


def test_insights_audit_path_defaults_to_bluecore_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AUDIT_FILE 未設定時は ~/.bluecore/logs 配下の絶対パスを解決すること。"""
    monkeypatch.setattr(insights_security_monitor, "AUDIT_FILE", None)
    monkeypatch.setattr(insights_security_monitor, "get_bluecore_dir", lambda: tmp_path)
    resolved = insights_security_monitor._resolve_audit_path()
    assert resolved == tmp_path / "logs" / "insaits_audit.jsonl"


def test_insights_security_monitor_skips_short_or_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(insights_security_monitor, "INSAITS_AVAILABLE", True)
    monkeypatch.setattr(
        insights_security_monitor,
        "insAItsMonitor",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("monitor should not be created")),
        raising=False,
    )
    monkeypatch.setattr(insights_security_monitor.sys, "stdin", io.StringIO("badjson"))

    with pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == 0


def test_run_insaits_scan_calls_monitor_with_session_name_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """insAItsMonitor が session_name のみ・余剰 kwargs なしで呼ばれることを保証する回帰テスト。

    0447f28 で insAItsMonitor(session_name=..., dev_mode=...) が
    insAItsMonitor(session_name=...) に修正されたが、既存スタブは
    `def __init__(self, *args, **kwargs)` で寛容なため dev_mode が
    再導入されても検知できなかった。本テストは呼び出し引数を厳密に
    assert し、余剰 kwargs の再導入を検知する。
    """
    monitor_calls: list[tuple[tuple, dict]] = []

    class RecordingMonitor:
        """コンストラクタ呼び出し引数を記録する SDK スタブ。"""

        def __init__(self, *args, **kwargs):
            monitor_calls.append((args, kwargs))

        def send_message(self, *args, **kwargs):  # noqa: ANN001
            return {"anomalies": []}

    monkeypatch.setattr(insights_security_monitor, "insAItsMonitor", RecordingMonitor, raising=False)

    insights_security_monitor._run_insaits_scan("hello world")

    assert len(monitor_calls) == 1
    args, kwargs = monitor_calls[0]
    assert args == ()
    assert kwargs == {"session_name": "claude-code-hook"}


def test_insights_security_monitor_reports_missing_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    warnings: list[str] = []
    monkeypatch.setattr(insights_security_monitor, "INSAITS_AVAILABLE", False)
    monkeypatch.setattr(
        insights_security_monitor,
        "log",
        SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg), debug=lambda *a, **k: None),
    )
    monkeypatch.setattr(
        insights_security_monitor.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})),
    )

    with pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == 0
    assert any("Not installed" in message for message in warnings)


@pytest.mark.parametrize(
    ("fail_mode", "harness", "expected_code"),
    [("open", "claude", 0), ("closed", "claude", 2), ("closed", "copilot", 0)],
)
def test_insights_security_monitor_handles_sdk_errors(
    monkeypatch: pytest.MonkeyPatch, fail_mode: str, harness: str, expected_code: int
) -> None:
    """SDK エラー時、fail-closed のブロックがハーネス別プロトコルで出力されること。

    Claude は stderr + exit 2、Copilot は permissionDecision: deny の
    stdout JSON + exit 0 でブロックする（exit 2 直書きの fail-open 回帰防止）。
    """

    class FailingMonitor:
        """send_message が常に失敗する SDK スタブ。"""
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, *args, **kwargs):  # noqa: ANN001
            raise RuntimeError("boom")

    warnings: list[str] = []
    monkeypatch.setattr(insights_security_monitor, "INSAITS_AVAILABLE", True)
    monkeypatch.setattr(insights_security_monitor, "insAItsMonitor", FailingMonitor, raising=False)
    monkeypatch.setattr(insights_security_monitor, "write_audit", lambda event: None)
    monkeypatch.setattr(
        insights_security_monitor,
        "log",
        SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg), debug=lambda *a, **k: None),
    )
    monkeypatch.setenv("INSAITS_FAIL_MODE", fail_mode)
    monkeypatch.setattr("bluecore.hooks.output_adapter.detect_harness", lambda: harness)
    monkeypatch.setattr(
        insights_security_monitor.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})),
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr), pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == expected_code
    if fail_mode == "open":
        assert any("SDK error" in message for message in warnings)
        assert stdout.getvalue() == ""
    elif harness == "claude":
        assert "blocking execution" in stderr.getvalue()
        assert stdout.getvalue() == ""
    else:
        payload = json.loads(stdout.getvalue())
        assert payload["permissionDecision"] == "deny"
        assert "blocking execution" in payload["permissionDecisionReason"]
        assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("anomalies", "harness", "expected_code", "critical"),
    [
        ([{"severity": "CRITICAL", "type": "LEAK", "details": "bad"}], "claude", 2, True),
        ([{"severity": "MEDIUM", "type": "NOTICE", "details": "warn"}], "claude", 0, False),
        ([{"severity": "CRITICAL", "type": "LEAK", "details": "bad"}], "copilot", 0, True),
        (
            [
                {
                    "severity": "CRITICAL",
                    "type": "TOOL_DESCRIPTION_DIVERGENCE",
                    "details": {"flags": ["hidden_instructions_in_message"]},
                }
            ],
            "claude",
            0,
            False,
        ),
        (
            [
                {
                    "severity": "CRITICAL",
                    "type": "TOOL_DESCRIPTION_DIVERGENCE",
                    "details": {"flags": ["hidden_instructions_in_message", "semantic_divergence"]},
                }
            ],
            "claude",
            2,
            True,
        ),
    ],
)
def test_insights_security_monitor_writes_audit_and_handles_anomalies(
    monkeypatch: pytest.MonkeyPatch,
    anomalies: list[dict[str, str]],
    harness: str,
    expected_code: int,
    critical: bool,
) -> None:
    """CRITICAL 異常のブロックがハーネス別プロトコルで出力されること。

    Claude は stderr + exit 2、Copilot は permissionDecision: deny の
    stdout JSON + exit 0 でブロックする（exit 2 直書きの fail-open 回帰防止）。
    """

    class Monitor:
        """固定の異常リストを返す SDK スタブ。"""
        def __init__(self, *args, **kwargs):
            pass

        def send_message(self, *args, **kwargs):  # noqa: ANN001
            return {"anomalies": anomalies}

    warnings: list[str] = []
    audits: list[dict[str, object]] = []
    monkeypatch.setattr(insights_security_monitor, "INSAITS_AVAILABLE", True)
    monkeypatch.setattr(insights_security_monitor, "insAItsMonitor", Monitor, raising=False)
    monkeypatch.setattr(
        insights_security_monitor,
        "log",
        SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg), debug=lambda *a, **k: None),
    )
    monkeypatch.setattr(insights_security_monitor, "write_audit", lambda event: audits.append(event))
    monkeypatch.setattr("bluecore.hooks.output_adapter.detect_harness", lambda: harness)
    monkeypatch.setattr(
        insights_security_monitor.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})),
    )

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr), pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == expected_code
    assert audits and audits[0]["anomaly_count"] == len(anomalies)
    assert audits[0]["anomaly_types"] == [item["type"] for item in anomalies]

    if not critical:
        assert any("Issues Detected" in message for message in warnings)
    elif harness == "claude":
        assert "Issues Detected" in stderr.getvalue()
        assert stdout.getvalue() == ""
    else:
        payload = json.loads(stdout.getvalue())
        assert payload["permissionDecision"] == "deny"
        assert "Issues Detected" in payload["permissionDecisionReason"]
        assert stderr.getvalue() == ""


@pytest.mark.parametrize(
    ("anomaly", "expected"),
    [
        ({"severity": "CRITICAL", "type": "CREDENTIAL_EXPOSURE", "details": {"flags": ["x"]}}, "CRITICAL"),
        (
            {
                "severity": "CRITICAL",
                "type": "TOOL_DESCRIPTION_DIVERGENCE",
                "details": {"flags": ["hidden_instructions_in_message"]},
            },
            "MEDIUM",
        ),
        (
            {
                "severity": "CRITICAL",
                "type": "TOOL_DESCRIPTION_DIVERGENCE",
                "details": {"flags": ["hidden_instructions_in_message", "semantic_divergence"]},
            },
            "CRITICAL",
        ),
        (
            {
                "severity": "CRITICAL",
                "type": "TOOL_DESCRIPTION_DIVERGENCE",
                "details": {"flags": ["goal_shift_after_tool_load", "hidden_instructions_in_message"]},
            },
            "MEDIUM",
        ),
        ({"severity": "CRITICAL", "type": "TOOL_DESCRIPTION_DIVERGENCE"}, "MEDIUM"),
        ({"severity": "CRITICAL", "type": "TOOL_DESCRIPTION_DIVERGENCE", "details": "not-a-dict"}, "MEDIUM"),
        (SimpleNamespace(severity="CRITICAL", type="TOOL_DESCRIPTION_DIVERGENCE", details=None), "MEDIUM"),
    ],
)
def test_effective_severity_downgrades_only_tool_description_divergence(
    anomaly: object, expected: str
) -> None:
    """TOOL_DESCRIPTION_DIVERGENCE の単一シグナルのみ MEDIUM に降格し、他 type・複数シグナルは維持される。"""
    assert insights_security_monitor._effective_severity(anomaly) == expected


def test_run_with_flags_build_env_and_resolve_command_branches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(run_with_flags, "REPO_ROOT", tmp_path)
    monkeypatch.setenv("PYTHONPATH", "base-path")
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

    env = run_with_flags.build_env()
    assert env["PYTHONPATH"] == f"{tmp_path / 'src'}{os.pathsep}base-path"
    assert "BLUECORE_HOOK_INPUT_TRUNCATED" not in env
    assert "BLUECORE_HOOK_INPUT_MAX_BYTES" not in env

    shell_script = tmp_path / "tool.sh"
    shell_script.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    assert run_with_flags.resolve_target_command(str(shell_script), []) == ["bash", str(shell_script)]

    relative_shell = plugin_root / "rel-tool.sh"
    relative_shell.write_text("#!/bin/sh\necho ok", encoding="utf-8")
    assert run_with_flags.resolve_target_command(
        "rel-tool.sh",
        ["y"],
        plugin_root=plugin_root,
    ) == ["bash", str(relative_shell), "y"]

    batch_script = tmp_path / "tool.cmd"
    batch_script.write_text("@echo off\necho ok", encoding="utf-8")
    monkeypatch.setattr(run_with_flags, "Path", type(tmp_path))
    monkeypatch.setattr(run_with_flags.os, "name", "nt", raising=False)
    assert run_with_flags.resolve_target_command(str(batch_script), []) == ["cmd", "/c", str(batch_script)]

    relative_cmd = plugin_root / "rel-tool.cmd"
    relative_cmd.write_text("@echo off\necho ok", encoding="utf-8")
    assert run_with_flags.resolve_target_command(
        "rel-tool.cmd",
        ["z"],
        plugin_root=plugin_root,
    ) == ["cmd", "/c", str(relative_cmd), "z"]

    original_resolve = run_with_flags.Path.resolve

    def fake_resolve(self, *args, **kwargs):  # noqa: ANN001
        if self.name == "bad-target":
            raise OSError("boom")
        return original_resolve(self, *args, **kwargs)

    monkeypatch.setattr(run_with_flags.Path, "resolve", fake_resolve)
    assert run_with_flags.resolve_target_command("bad-target", ["x"]) == [sys.executable, "-m", "bad-target", "x"]


def test_run_with_flags_entrypoint_exits_one_when_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_with_flags.sys, "argv", ["run_with_flags.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.run_with_flags", run_name="__main__")

    # 引数不足時は exit 1 で終了する（stdin の読み取りは不要）
    assert excinfo.value.code == 1


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


def test_insights_security_monitor_import_reload_and_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_module = types.ModuleType("insa_its")

    class DummyMonitor:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def send_message(self, *args, **kwargs):  # noqa: ANN001
            return {"anomalies": []}

    fake_module.insAItsMonitor = DummyMonitor
    monkeypatch.setitem(sys.modules, "insa_its", fake_module)

    module = importlib.reload(insights_security_monitor)
    monkeypatch.setattr(module, "AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    monkeypatch.setattr(module.sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})))

    with pytest.raises(SystemExit) as excinfo:
        module.main()

    assert excinfo.value.code == 0
    assert (tmp_path / "audit.jsonl").exists()

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "argv", ["insights_security_monitor.py"])

    with pytest.raises(SystemExit) as entry_excinfo:
        runpy.run_module("bluecore.hooks.insights_security_monitor", run_name="__main__")

    assert entry_excinfo.value.code == 0


def test_insights_extract_content_list_and_str() -> None:
    """content が list/str いずれの形でもテキストを抽出する。"""
    from bluecore.hooks.insights_security_monitor import extract_content

    text_list, _ = extract_content({"content": [{"type": "text", "text": "hello"}]})
    assert "hello" in text_list
    text_str, _ = extract_content({"content": "world"})
    assert text_str == "world"


def test_insights_extract_content_absent_and_non_text() -> None:
    """content も対象ツールも無い／content が非リスト非文字列なら空テキスト。"""
    from bluecore.hooks.insights_security_monitor import extract_content

    assert extract_content({}) == ("", "")
    text, _ = extract_content({"content": 123})
    assert text == ""


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
