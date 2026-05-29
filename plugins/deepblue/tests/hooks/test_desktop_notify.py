"""desktop_notify フックのテスト。"""

from __future__ import annotations

import json
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from devgear.hooks import desktop_notify as hook


class TestExtractSummary:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            (None, "Done"),
            ("", "Done"),
            ("   \n  ", "Done"),
            ("  first line\nsecond line", "first line"),
            ("ご質問ありがとうございます。\n  認証MW バグ。", "認証MW バグ"),
            ("x" * 101, "x" * 100 + "..."),
        ],
    )
    def test_extract_summary(self, message: str | None, expected: str) -> None:
        assert hook.extract_summary(message) == expected


class TestFindPowerShell:
    def test_returns_first_working_candidate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd[0])
            if "pwsh.exe" in cmd[0]:
                raise FileNotFoundError("missing")
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.find_powershell() == "powershell.exe"
        assert calls[:2] == ["pwsh.exe", "powershell.exe"]

    def test_returns_none_when_all_candidates_fail(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("missing")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.find_powershell() is None


class TestIsWsl:
    def test_returns_cached_value_and_non_linux_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_is_wsl", None)
        monkeypatch.setattr(hook, "IS_LINUX", False)

        assert hook.is_wsl() is False

        monkeypatch.setattr(hook, "_is_wsl", True)
        assert hook.is_wsl() is True

    def test_handles_proc_version_read_errors(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_is_wsl", None)
        monkeypatch.setattr(hook, "IS_LINUX", True)

        def fake_read_text(self, *args, **kwargs):  # noqa: ANN001
            raise OSError("boom")

        monkeypatch.setattr(hook.Path, "read_text", fake_read_text)

        assert hook.is_wsl() is False

    def test_detects_wsl_from_proc_version(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_is_wsl", None)
        monkeypatch.setattr(hook, "IS_LINUX", True)
        monkeypatch.setattr(hook.Path, "read_text", lambda self, *args, **kwargs: "Linux Microsoft")  # noqa: ARG005

        assert hook.is_wsl() is True


class TestNotifyWindows:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return SimpleNamespace(returncode=0, stderr="")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.notify_windows("pwsh", "title", "body") == {"success": True, "reason": None}
        assert calls[0][0] == "pwsh"

    def test_failure_with_stderr(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stderr="boom")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.notify_windows("pwsh", "title", "body") == {"success": False, "reason": "boom"}

    def test_timeout_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        result = hook.notify_windows("pwsh", "title", "body")

        assert result["success"] is False
        assert "timed out" in result["reason"]


class TestNotifyMacOS:
    def test_logs_when_osascript_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []

        def fake_run(cmd, **kwargs):
            raise FileNotFoundError("missing osascript")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)
        monkeypatch.setattr(hook, "log", messages.append)

        hook.notify_macos("title", "body")

        assert any("osascript failed" in message for message in messages)


class TestRun:
    def test_macos_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(hook, "IS_MACOS", True)
        monkeypatch.setattr(hook, "is_wsl", lambda: False)
        monkeypatch.setattr(hook, "notify_macos", lambda title, body: calls.append((title, body)))

        raw = json.dumps({"last_assistant_message": "first line\nsecond"})
        assert hook.run(raw) == raw
        assert calls == [(hook.TITLE, "first line")]

    def test_wsl_burnttoast_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda: "pwsh")
        monkeypatch.setattr(
            hook,
            "notify_windows",
            lambda *args, **kwargs: {"success": False, "reason": "BurntToast module not found"},
        )
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) == raw
        assert any("BurntToast" in message for message in messages)

    def test_wsl_success_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda: "pwsh")
        monkeypatch.setattr(hook, "notify_windows", lambda *args, **kwargs: {"success": True, "reason": None})
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) == raw
        assert messages == []

    def test_wsl_without_powershell_logs_tip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda: None)
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) == raw
        assert any("PowerShell" in message for message in messages)

    def test_wsl_generic_failure_logs_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda: "pwsh")
        monkeypatch.setattr(hook, "notify_windows", lambda *args, **kwargs: {"success": False, "reason": "boom"})
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) == raw
        assert any("Notification failed: boom" in message for message in messages)

    def test_exception_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []

        def raise_error(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", raise_error)
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) == raw
        assert any("Error: boom" in message for message in messages)

    def test_main_passthrough(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: "raw")
        monkeypatch.setattr(hook, "run", lambda raw: raw + "-out")

        assert hook.main() == 0
        assert capsys.readouterr().out == "raw-out"

    def test_main_returns_zero_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert hook.main() == 0

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("devgear.hooks.hook_common.read_raw_stdin", lambda: json.dumps({"last_assistant_message": "hello"}))
        monkeypatch.setattr("devgear.lib.core_utils.IS_MACOS", False)
        monkeypatch.setattr("devgear.lib.core_utils.IS_LINUX", False)

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("devgear.hooks.desktop_notify", run_name="__main__")

        assert excinfo.value.code == 0
