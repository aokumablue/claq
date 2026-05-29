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
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgear.hooks import insights_security_monitor as insights_security_monitor
from devgear.hooks import pre_bash_commit_quality as pre_bash_commit_quality
from devgear.hooks import run_with_flags as run_with_flags
from devgear.hooks import session_start as session_start


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
    ) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["input"] = input
        captured["env"] = env
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
    assert "DEVGEAR_HOOK_INPUT_TRUNCATED" not in env
    assert "DEVGEAR_HOOK_INPUT_MAX_BYTES" not in env
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
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("slim-content", encoding="utf-8")

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
    monkeypatch.setattr(
        session_start.Settings,
        "load",
        lambda: SimpleNamespace(slim=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(session_start, "_SLIM_SKILL_PATH", skill_file)

    payload = json.loads(session_start.run("{not-json"))
    assert "slim-content" in payload["hookSpecificOutput"]["additionalContext"]


def test_pre_bash_commit_quality_detects_file_issues_and_commit_message_rules(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = "\n".join(
        [
            'console.log("hi")',
            "// console.log('commented')",
            "debugger",
            "// TODO: clean this up",
            "// TODO: #123 tracked",
            'const api_key = "abc";',
        ]
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_file_content", lambda path: content)

    issues = pre_bash_commit_quality.find_file_issues("src/app.js")

    assert {issue["type"] for issue in issues} == {"console.log", "debugger", "todo", "secret"}
    assert [issue["line"] for issue in issues if issue["type"] == "console.log"] == [1]
    assert [issue["line"] for issue in issues if issue["type"] == "todo"] == [4]

    assert pre_bash_commit_quality.validate_commit_message("git status") is None
    message = pre_bash_commit_quality.validate_commit_message('git commit -m "feat(core): Add feature."')
    assert message is not None
    assert message["message"] == "feat(core): Add feature."
    assert {issue["type"] for issue in message["issues"]} == {"capitalization", "punctuation"}


def test_pre_bash_commit_quality_helpers_handle_subprocess_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 1, stdout="", stderr=""),
    )

    assert pre_bash_commit_quality.get_staged_files() == []
    assert pre_bash_commit_quality.get_staged_file_content("src/app.js") is None
    assert pre_bash_commit_quality.should_check_file("src/app.py")
    assert not pre_bash_commit_quality.should_check_file("src/app.txt")


def test_pre_bash_commit_quality_helpers_return_success_outputs(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(command: list[str], *, capture_output: bool, text: bool, check: bool) -> subprocess.CompletedProcess[str]:
        if command[:2] == ["git", "diff"]:
            return subprocess.CompletedProcess(command, 0, stdout="src/app.py\nsrc/tool.ts\n", stderr="")
        if command[:2] == ["git", "show"]:
            return subprocess.CompletedProcess(command, 0, stdout="file content", stderr="")
        raise AssertionError(f"unexpected command: {command}")

    monkeypatch.setattr(pre_bash_commit_quality.subprocess, "run", fake_run)

    assert pre_bash_commit_quality.get_staged_files() == ["src/app.py", "src/tool.ts"]
    assert pre_bash_commit_quality.get_staged_file_content("src/app.js") == "file content"


def test_pre_bash_commit_quality_finds_parser_and_reading_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_file_content", lambda path: (_ for _ in ()).throw(RuntimeError("boom")))
    assert pre_bash_commit_quality.find_file_issues("src/app.js") == []

    long_message = "git commit -m \"bad message with no conventional format and a very long subject line that keeps going.\""
    parsed = pre_bash_commit_quality.validate_commit_message(long_message)
    assert parsed is not None
    assert {issue["type"] for issue in parsed["issues"]} >= {"format", "length"}


def test_pre_bash_commit_quality_run_wrapper_and_main_success(monkeypatch: pytest.MonkeyPatch) -> None:
    assert pre_bash_commit_quality.run("payload") == pre_bash_commit_quality.evaluate("payload")

    monkeypatch.setattr("devgear.hooks.hook_common.read_raw_stdin", lambda: "payload")
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
    monkeypatch.setattr(pre_bash_commit_quality, "should_check_file", lambda path: True)

    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path: [{"severity": "error", "line": 1, "message": "boom"}],
    )
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)
    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 2}

    warning_logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", warning_logs.append)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path: [])
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
    monkeypatch.setattr(pre_bash_commit_quality, "should_check_file", lambda path: True)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "find_file_issues",
        lambda path: [
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
        "devgear.hooks.hook_common.read_raw_stdin",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    assert pre_bash_commit_quality.main() == 0


def test_insights_security_monitor_helpers_and_audit_errors(monkeypatch: pytest.MonkeyPatch) -> None:
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
    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    insights_security_monitor.write_audit({"tool": "Write"})
    assert any("Failed to write audit log" in message for message in warnings)


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
    ("fail_mode", "expected_code"),
    [("open", 0), ("closed", 2)],
)
def test_insights_security_monitor_handles_sdk_errors(
    monkeypatch: pytest.MonkeyPatch, fail_mode: str, expected_code: int
) -> None:
    class FailingMonitor:
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
    monkeypatch.setattr(
        insights_security_monitor.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})),
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout), pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == expected_code
    if expected_code == 0:
        assert any("SDK error" in message for message in warnings)
        assert stdout.getvalue() == ""
    else:
        assert "blocking execution" in stdout.getvalue()


@pytest.mark.parametrize(
    ("anomalies", "expected_code", "critical"),
    [
        ([{"severity": "CRITICAL", "type": "LEAK", "details": "bad"}], 2, True),
        ([{"severity": "MEDIUM", "type": "NOTICE", "details": "warn"}], 0, False),
    ],
)
def test_insights_security_monitor_writes_audit_and_handles_anomalies(
    monkeypatch: pytest.MonkeyPatch, anomalies: list[dict[str, str]], expected_code: int, critical: bool
) -> None:
    class Monitor:
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
    monkeypatch.setattr(
        insights_security_monitor.sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hello world"}})),
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout), pytest.raises(SystemExit) as excinfo:
        insights_security_monitor.main()

    assert excinfo.value.code == expected_code
    assert audits and audits[0]["anomaly_count"] == len(anomalies)
    assert audits[0]["anomaly_types"] == [item["type"] for item in anomalies]

    if critical:
        assert "Issues Detected" in stdout.getvalue()
    else:
        assert any("Issues Detected" in message for message in warnings)


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
    assert "DEVGEAR_HOOK_INPUT_TRUNCATED" not in env
    assert "DEVGEAR_HOOK_INPUT_MAX_BYTES" not in env

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
        runpy.run_module("devgear.hooks.run_with_flags", run_name="__main__")

    # 引数不足時は exit 1 で終了する（stdin の読み取りは不要）
    assert excinfo.value.code == 1


def test_pre_bash_commit_quality_helpers_and_pass_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pre_bash_commit_quality.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("missing")),
    )

    assert pre_bash_commit_quality.get_staged_files() == []
    assert pre_bash_commit_quality.get_staged_file_content("src/app.js") is None
    assert pre_bash_commit_quality.find_file_issues("src/app.js") == []

    logs: list[str] = []
    monkeypatch.setattr(pre_bash_commit_quality, "log", logs.append)
    monkeypatch.setattr(
        pre_bash_commit_quality,
        "parse_json_object",
        lambda raw: {"tool_input": {"command": "git commit -m 'feat(core): add'" }},
    )
    monkeypatch.setattr(pre_bash_commit_quality, "get_staged_files", lambda: ["src/app.js"])
    monkeypatch.setattr(pre_bash_commit_quality, "should_check_file", lambda path: True)
    monkeypatch.setattr(pre_bash_commit_quality, "find_file_issues", lambda path: [])
    monkeypatch.setattr(pre_bash_commit_quality, "validate_commit_message", lambda command: None)

    assert pre_bash_commit_quality.evaluate("payload") == {"output": "payload", "exitCode": 0}
    assert any("PASS: All checks passed" in message for message in logs)


def test_pre_bash_commit_quality_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.hooks.pre_bash_commit_quality", run_name="__main__")

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
        runpy.run_module("devgear.hooks.insights_security_monitor", run_name="__main__")

    assert entry_excinfo.value.code == 0
