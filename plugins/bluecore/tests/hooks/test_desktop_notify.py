"""desktop_notify フックのテスト。"""

from __future__ import annotations

import json
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from bluecore.hooks import desktop_notify as hook


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


class TestFindPowerShellBudget:
    """find_powershell()のdeadlineベース予算管理に対する回帰テスト群。

    候補ごとのプローブがtimeoutいっぱいまでブロックする最悪ケースを
    フェイク時計（time.monotonicのモック）で決定的にシミュレートし、
    合計プローブ時間が指定した予算を超過しないことを検証する。
    """

    @staticmethod
    def _install_fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
        clock = {"t": 0.0}
        monkeypatch.setattr(hook.time, "monotonic", lambda: clock["t"])
        return clock

    def test_total_probe_time_does_not_exceed_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全候補が最悪ケース(timeoutいっぱい)でブロックしても、合計プローブ時間が予算内に収まる。"""
        clock = self._install_fake_clock(monkeypatch)
        observed_timeouts: list[float] = []

        def fake_run(cmd, **kwargs):
            timeout = kwargs["timeout"]
            observed_timeouts.append(timeout)
            clock["t"] += timeout
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=timeout)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        budget = 4.0
        deadline = hook.time.monotonic() + budget

        assert hook.find_powershell(deadline) is None
        assert sum(observed_timeouts) <= budget
        # POWERSHELL_PROBE_TIMEOUT(3秒)が予算(4秒)を上回るため、
        # 4候補全てはプローブされず途中で打ち切られる。
        assert len(observed_timeouts) < 4

    def test_each_probe_timeout_never_exceeds_probe_cap(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """予算が十分でも、1候補あたりのtimeoutはPOWERSHELL_PROBE_TIMEOUTを超えない。"""
        clock = self._install_fake_clock(monkeypatch)
        observed_timeouts: list[float] = []

        def fake_run(cmd, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            raise FileNotFoundError("missing")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        deadline = hook.time.monotonic() + 100.0  # 潤沢な予算
        clock["t"] = 0.0

        assert hook.find_powershell(deadline) is None
        assert observed_timeouts
        assert all(t <= hook.POWERSHELL_PROBE_TIMEOUT for t in observed_timeouts)

    def test_probe_timeout_shrinks_with_remaining_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """残り予算がPOWERSHELL_PROBE_TIMEOUTより小さくなった候補には、残り予算分だけが配分される。"""
        clock = self._install_fake_clock(monkeypatch)
        observed_timeouts: list[float] = []

        def fake_run(cmd, **kwargs):
            timeout = kwargs["timeout"]
            observed_timeouts.append(timeout)
            clock["t"] += timeout
            raise FileNotFoundError("missing")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        deadline = hook.time.monotonic() + 4.0  # 3秒キャップの候補が2つ入らない予算

        assert hook.find_powershell(deadline) is None
        assert observed_timeouts[0] == pytest.approx(hook.POWERSHELL_PROBE_TIMEOUT)
        assert observed_timeouts[1] == pytest.approx(1.0)
        assert len(observed_timeouts) == 2

    def test_stops_probing_when_deadline_already_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """呼び出し時点で既にdeadlineを過ぎている場合、1候補もプローブしない。"""
        clock = self._install_fake_clock(monkeypatch)
        clock["t"] = 10.0

        def fake_run(cmd, **kwargs):
            raise AssertionError("deadline超過後はsubprocessを呼び出すべきではない")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.find_powershell(deadline=5.0) is None

    def test_none_deadline_defaults_to_default_notification_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deadline未指定時はDEFAULT_NOTIFICATION_TIMEOUT秒後を締切として使う。"""
        self._install_fake_clock(monkeypatch)
        captured_timeouts: list[float] = []

        def fake_run(cmd, **kwargs):
            captured_timeouts.append(kwargs["timeout"])
            return SimpleNamespace(returncode=0)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.find_powershell() == "pwsh.exe"
        assert captured_timeouts[0] == pytest.approx(hook.POWERSHELL_PROBE_TIMEOUT)


class TestNotificationTimeout:
    def test_returns_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BLUECORE_DESKTOP_NOTIFY_TIMEOUT", raising=False)

        assert hook._notification_timeout() == hook.DEFAULT_NOTIFICATION_TIMEOUT

    def test_returns_default_when_env_invalid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUECORE_DESKTOP_NOTIFY_TIMEOUT", "not-a-number")

        assert hook._notification_timeout() == hook.DEFAULT_NOTIFICATION_TIMEOUT

    def test_returns_default_when_env_non_positive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUECORE_DESKTOP_NOTIFY_TIMEOUT", "0")

        assert hook._notification_timeout() == hook.DEFAULT_NOTIFICATION_TIMEOUT

    def test_returns_default_when_env_infinite(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUECORE_DESKTOP_NOTIFY_TIMEOUT", "inf")

        assert hook._notification_timeout() == hook.DEFAULT_NOTIFICATION_TIMEOUT

    def test_returns_env_value_when_valid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BLUECORE_DESKTOP_NOTIFY_TIMEOUT", "2.5")

        assert hook._notification_timeout() == pytest.approx(2.5)


class TestRemainingTimeout:
    def test_returns_positive_remainder_before_deadline(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook.time, "monotonic", lambda: 10.0)

        assert hook._remaining_timeout(15.0) == pytest.approx(5.0)

    def test_returns_zero_when_deadline_passed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook.time, "monotonic", lambda: 20.0)

        assert hook._remaining_timeout(15.0) == 0.0


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


class TestDecodeStderr:
    def test_bytes_decoded_as_utf8(self) -> None:
        assert hook._decode_stderr("日本語".encode()) == "日本語"

    def test_invalid_utf8_bytes_replaced_not_raised(self) -> None:
        result = hook._decode_stderr(b"\xff\xfe")
        assert isinstance(result, str)

    def test_str_passthrough(self) -> None:
        assert hook._decode_stderr("already decoded") == "already decoded"

    def test_none_passthrough(self) -> None:
        assert hook._decode_stderr(None) is None


class TestNotifyWindows:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return SimpleNamespace(returncode=0, stderr=b"")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.notify_windows("pwsh", "title", "body") == {"success": True, "reason": None}
        assert calls[0][0][0] == "pwsh"

    def test_does_not_request_text_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdout/stderrをPython側でデコードさせず、生バイト列で受け取ること（text=Trueを使わない）。"""
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(returncode=0, stderr=b"")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        hook.notify_windows("pwsh", "title", "body")

        assert "text" not in calls[0]
        assert calls[0]["capture_output"] is True

    def test_failure_with_stderr_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stderrがUTF-8として正常デコード可能なバイト列のケース。"""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stderr=b"boom")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.notify_windows("pwsh", "title", "body") == {"success": False, "reason": "boom"}

    def test_failure_with_invalid_utf8_stderr_does_not_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stderrがUTF-8として不正なバイト列でも例外を送出せず、置換文字でデコードすること。"""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=1, stderr=b"\xff\xfe\x00invalid")

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        result = hook.notify_windows("pwsh", "title", "body")

        assert result["success"] is False
        assert isinstance(result["reason"], str)

    def test_failure_with_none_stderr_falls_back_to_exit_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stderrがNoneの場合はexitコードを使ったメッセージにフォールバックすること。"""

        def fake_run(cmd, **kwargs):
            return SimpleNamespace(returncode=2, stderr=None)

        monkeypatch.setattr(hook.subprocess, "run", fake_run)

        assert hook.notify_windows("pwsh", "title", "body") == {"success": False, "reason": "exit 2"}

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


class TestParseStopInput:
    def test_empty_input_returns_empty_dict(self) -> None:
        """空・空白のみの stdin は空 dict にする。"""
        assert hook._parse_stop_input("") == {}
        assert hook._parse_stop_input("   \n") == {}

    def test_invalid_or_non_object_json_returns_empty_dict(self) -> None:
        """不正 JSON・非 object は parse 失敗として空 dict にする。"""
        assert hook._parse_stop_input("not-json") == {}
        assert hook._parse_stop_input("[1]") == {}
        assert hook._parse_stop_input("null") == {}
        assert hook._parse_stop_input("{}") == {}


class TestAssistantMessage:
    def test_returns_none_when_no_usable_field(self) -> None:
        """候補キーが無い・空・非文字列なら None を返す。"""
        assert hook._assistant_message({}) is None
        assert hook._assistant_message({"last_assistant_message": ""}) is None
        assert hook._assistant_message({"last_assistant_message": "   "}) is None
        assert hook._assistant_message({"response": 1, "lastAssistantMessage": None}) is None

    def test_prefers_first_nonempty_string_key(self) -> None:
        """定義順で最初の非空文字列を返す。"""
        assert hook._assistant_message({"lastAssistantMessage": "alt"}) == "alt"
        assert hook._assistant_message({"response": "body"}) == "body"


class TestRun:
    def test_invalid_json_notifies_with_done(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 object / 不正 JSON でもサマリーは Done になり通知する。"""
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(hook, "IS_MACOS", True)
        monkeypatch.setattr(hook, "is_wsl", lambda: False)
        monkeypatch.setattr(
            hook, "notify_macos", lambda title, body, **kwargs: calls.append((title, body))  # noqa: ARG005
        )

        assert hook.run("not-json") is None
        assert calls == [(hook.TITLE, "Done")]

    def test_macos_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[str, str]] = []
        monkeypatch.setattr(hook, "IS_MACOS", True)
        monkeypatch.setattr(hook, "is_wsl", lambda: False)
        monkeypatch.setattr(
            hook, "notify_macos", lambda title, body, **kwargs: calls.append((title, body))  # noqa: ARG005
        )

        raw = json.dumps({"last_assistant_message": "first line\nsecond"})
        assert hook.run(raw) is None
        assert calls == [(hook.TITLE, "first line")]

    def test_macos_branch_passes_remaining_budget_as_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """macOS通知には、予算(_notification_timeout())から算出した残り時間がtimeoutとして渡される。"""
        monkeypatch.setattr(hook.time, "monotonic", lambda: 0.0)
        monkeypatch.setattr(hook, "IS_MACOS", True)
        monkeypatch.setattr(hook, "is_wsl", lambda: False)
        monkeypatch.setattr(hook, "_notification_timeout", lambda: 3.0)
        captured: dict[str, float] = {}
        monkeypatch.setattr(
            hook, "notify_macos", lambda title, body, **kwargs: captured.update(kwargs)  # noqa: ARG005
        )

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert captured["timeout"] == pytest.approx(3.0)

    def test_macos_branch_skips_notify_when_budget_already_exhausted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """deadline計算後に予算を使い切っていた場合、macOS通知は送信されない。"""
        clock_values = iter([0.0, 10.0])
        monkeypatch.setattr(hook.time, "monotonic", lambda: next(clock_values))
        monkeypatch.setattr(hook, "IS_MACOS", True)
        monkeypatch.setattr(hook, "is_wsl", lambda: False)
        monkeypatch.setattr(hook, "_notification_timeout", lambda: 1.0)
        called: list[tuple] = []
        monkeypatch.setattr(hook, "notify_macos", lambda *args, **kwargs: called.append((args, kwargs)))

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert called == []

    def test_wsl_passes_deadline_to_find_powershell(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """WSL経路ではfind_powershellに_notification_timeout()由来のdeadlineが渡される。"""
        monkeypatch.setattr(hook.time, "monotonic", lambda: 100.0)
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "_notification_timeout", lambda: 7.5)
        captured: dict[str, float] = {}

        def fake_find_powershell(deadline):
            captured["deadline"] = deadline
            return None

        monkeypatch.setattr(hook, "find_powershell", fake_find_powershell)
        monkeypatch.setattr(hook, "log", lambda *args, **kwargs: None)

        raw = json.dumps({"last_assistant_message": "hello"})
        hook.run(raw)
        assert captured["deadline"] == pytest.approx(107.5)

    def test_wsl_skips_notify_when_budget_exhausted_during_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """PowerShell探索で予算を使い切った場合、notify_windowsは呼ばれずrawを返す。"""
        clock = {"t": 0.0}
        monkeypatch.setattr(hook.time, "monotonic", lambda: clock["t"])
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "_notification_timeout", lambda: 1.0)

        def fake_find_powershell(deadline):
            clock["t"] += 2.0  # 予算を使い果たしてから見つかったケースを模擬
            return "pwsh"

        monkeypatch.setattr(hook, "find_powershell", fake_find_powershell)
        called: list[tuple] = []
        monkeypatch.setattr(hook, "notify_windows", lambda *args, **kwargs: called.append((args, kwargs)))

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert called == []

    def test_wsl_passes_remaining_budget_as_notify_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """notify_windowsには予算の残り時間がtimeoutとして渡される。"""
        monkeypatch.setattr(hook.time, "monotonic", lambda: 0.0)
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "_notification_timeout", lambda: 4.0)
        monkeypatch.setattr(hook, "find_powershell", lambda deadline: "pwsh")
        captured: dict[str, float] = {}
        monkeypatch.setattr(
            hook,
            "notify_windows",
            lambda ps, title, body, **kwargs: captured.update(kwargs) or {"success": True, "reason": None},
        )

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert captured["timeout"] == pytest.approx(4.0)

    def test_wsl_burnttoast_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda *args, **kwargs: "pwsh")  # noqa: ARG005
        monkeypatch.setattr(
            hook,
            "notify_windows",
            lambda *args, **kwargs: {"success": False, "reason": "BurntToast module not found"},
        )
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert any("BurntToast" in message for message in messages)

    def test_wsl_success_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda *args, **kwargs: "pwsh")  # noqa: ARG005
        monkeypatch.setattr(hook, "notify_windows", lambda *args, **kwargs: {"success": True, "reason": None})
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert messages == []

    def test_wsl_without_powershell_logs_tip(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda *args, **kwargs: None)  # noqa: ARG005
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert any("PowerShell" in message for message in messages)

    def test_wsl_generic_failure_logs_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", lambda: True)
        monkeypatch.setattr(hook, "find_powershell", lambda *args, **kwargs: "pwsh")  # noqa: ARG005
        monkeypatch.setattr(hook, "notify_windows", lambda *args, **kwargs: {"success": False, "reason": "boom"})
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert any("Notification failed: boom" in message for message in messages)

    def test_exception_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []

        def raise_error(*_args, **_kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(hook, "IS_MACOS", False)
        monkeypatch.setattr(hook, "is_wsl", raise_error)
        monkeypatch.setattr(hook, "log", messages.append)

        raw = json.dumps({"last_assistant_message": "hello"})
        assert hook.run(raw) is None
        assert any("Error: boom" in message for message in messages)

    def test_main_does_not_echo_stdout(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: "raw")
        called: list[str] = []
        monkeypatch.setattr(hook, "run", lambda raw: called.append(raw))

        assert hook.main() == 0
        assert called == ["raw"]
        assert capsys.readouterr().out == ""

    def test_main_returns_zero_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

        assert hook.main() == 0

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: json.dumps({"last_assistant_message": "hello"}))
        monkeypatch.setattr("bluecore.lib.core_utils.IS_MACOS", False)
        monkeypatch.setattr("bluecore.lib.core_utils.IS_LINUX", False)

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.hooks.desktop_notify", run_name="__main__")

        assert excinfo.value.code == 0


def test_find_powershell_all_candidates_nonzero(monkeypatch) -> None:
    """全候補が非0終了なら None を返す。"""
    from types import SimpleNamespace

    from bluecore.hooks import desktop_notify

    monkeypatch.setattr(desktop_notify.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert desktop_notify.find_powershell() is None
