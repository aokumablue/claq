"""hook_common のフック出力ヘルパー（SessionStart/UserPromptSubmit/PostToolUse）と
stdin 読み取りガード（TTY/タイムアウト/バイト上限）のテスト。
"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from bluecore.hooks import hook_common
from bluecore.hooks.hook_common import (
    detach_process,
    emit_post_tool_use_output,
    emit_session_start_output,
    emit_user_prompt_submit_output,
    print_session_start_output,
)


def _parse_session_start(output: str) -> dict:
    """出力が有効な SessionStart hookSpecificOutput JSON かを検証して返す。"""
    payload = json.loads(output)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert "additionalContext" in inner
    return inner


class TestEmitSessionStartOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "",
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "a" * 5000,
        ],
        ids=["empty", "simple", "newlines", "unicode", "long"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        result = emit_session_start_output(additional_context)
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == additional_context

    def test_default_empty_context(self) -> None:
        result = emit_session_start_output()
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == ""

    def test_is_string(self) -> None:
        assert isinstance(emit_session_start_output(), str)

    def test_no_trailing_newline(self) -> None:
        result = emit_session_start_output()
        assert not result.endswith("\n")


class TestEmitUserPromptSubmitOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "<mem-context>関連メモリ</mem-context>",
        ],
        ids=["simple", "newlines", "unicode", "mem-context"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        payload = json.loads(emit_user_prompt_submit_output(additional_context))
        inner = payload["hookSpecificOutput"]
        assert inner["hookEventName"] == "UserPromptSubmit"
        assert inner["additionalContext"] == additional_context

    def test_merged_output_has_top_level_additional_context_but_no_event_name(self) -> None:
        """合併出力はトップレベル additionalContext を持つが hookEventName は持たない。"""
        payload = json.loads(emit_user_prompt_submit_output("ctx"))
        assert "hookEventName" not in payload
        assert payload["additionalContext"] == "ctx"

    def test_unicode_not_escaped(self) -> None:
        assert "日本語" in emit_user_prompt_submit_output("日本語")


class TestEmitPostToolUseOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
        ],
        ids=["simple", "newlines", "unicode"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        payload = json.loads(emit_post_tool_use_output(additional_context))
        inner = payload["hookSpecificOutput"]
        assert inner["hookEventName"] == "PostToolUse"
        assert inner["additionalContext"] == additional_context


class TestPrintSessionStartOutput:
    def test_prints_to_stdout(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output("hello")
        output = buf.getvalue()
        inner = _parse_session_start(output.strip())
        assert inner["additionalContext"] == "hello"

    def test_default_empty_context(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output()
        inner = _parse_session_start(buf.getvalue().strip())
        assert inner["additionalContext"] == ""


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data[:n] if n >= 0 else self._data


class _FakeStdin:
    """isatty() を備えた stdin の代替オブジェクト。"""

    def __init__(self, text: str, *, tty: bool = False) -> None:
        self._tty = tty
        self.buffer = _FakeBuffer(text.encode("utf-8"))
        self.read_called = False

    def isatty(self) -> bool:
        return self._tty

    def read(self, n: int = -1) -> str:
        raise AssertionError("バイト読みでは text read を使わない")


def _patch_select_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """select を常に ready 扱いへ差し替える（フェイク stdin は実 fd を持たないため）。"""
    monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: (r, [], []))


class TestStdinReady:
    """_stdin_ready の TTY/タイムアウトガードのテスト（旧 launcher._read_stdin 相当）。"""

    def test_tty_returns_false_without_calling_select(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        def fail_select(*args):  # noqa: ANN002
            raise AssertionError("select must not be called for tty stdin")

        monkeypatch.setattr(hook_common.select, "select", fail_select)

        assert hook_common._stdin_ready() is False

    def test_ready_pipe_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        _patch_select_ready(monkeypatch)

        assert hook_common._stdin_ready() is True

    def test_timeout_returns_false_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """select タイムアウト時は stderr 警告のうえ False を返す（NG-B1 回帰）。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        assert hook_common._stdin_ready() is False
        assert "リダイレクト漏れ" in capsys.readouterr().err


class TestReadRawStdin:
    """read_raw_stdin のバイト単位制限・stdin ガードのテスト。"""

    def test_limits_by_bytes_not_chars_with_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer 付き stdin はバイト単位で読み取りを制限する。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("あ" * 10))
        _patch_select_ready(monkeypatch)

        result = hook_common.read_raw_stdin(max_bytes=10)

        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）
        assert result == "あああ�"

    def test_text_stdin_is_byte_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer を持たない stdin（io.StringIO 等）もバイト換算で切り捨てる。"""
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("あ" * 10))
        _patch_select_ready(monkeypatch)

        result = hook_common.read_raw_stdin(max_bytes=10)

        assert len(result.encode("utf-8")) <= 12  # 置換文字を含む 10 バイト相当
        assert result.startswith("あああ")

    def test_small_input_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """制限未満の入力はそのまま返る。"""
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("hello"))
        _patch_select_ready(monkeypatch)

        assert hook_common.read_raw_stdin() == "hello"

    def test_tty_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        assert hook_common.read_raw_stdin() == ""

    def test_select_timeout_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_stdin = _FakeStdin("payload")
        monkeypatch.setattr(hook_common.sys, "stdin", fake_stdin)
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        assert hook_common.read_raw_stdin() == ""
        assert fake_stdin.read_called is False


class TestReadRawStdinWithTruncation:
    """read_raw_stdin_with_truncation の切り捨て判定・stdin ガードのテスト。"""

    def test_no_truncation_when_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("short"))
        _patch_select_ready(monkeypatch)

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == "short"
        assert truncated is False

    def test_truncates_when_exceeding_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("a" * 20))
        _patch_select_ready(monkeypatch)

        text, truncated = hook_common.read_raw_stdin_with_truncation(max_bytes=10)

        assert text == "a" * 10
        assert truncated is True

    def test_tty_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False

    def test_select_timeout_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False


class TestDetachProcess:
    """detach_process の一時ファイル経由 stdin 引き渡し・エラー処理のテスト。

    呼び出し元は launcher.py の `--bg` 実行 1 箇所のみ（`_run_background`）。
    """

    def test_launches_detached_process_and_cleans_up_tmp_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        captured = {}
        written_stdin_content = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["cmd"] = cmd
                captured["env"] = env
                captured["start_new_session"] = start_new_session
                stdin.seek(0)
                written_stdin_content["text"] = stdin.read()

        monkeypatch.setattr(hook_common.subprocess, "Popen", _FakePopen)

        result = detach_process(["python3", "-m", "bluecore.mem.cli", "observe"], "raw-payload", env={"X": "1"})

        assert result is True
        assert captured["cmd"][:3] == [hook_common.sys.executable, "-c", hook_common._WATCHDOG_SCRIPT]
        assert captured["cmd"][3:5] == [
            str(hook_common.DETACH_TIMEOUT_SECONDS),
            str(hook_common._DETACH_KILL_AFTER_SECONDS),
        ]
        assert captured["cmd"][5:] == ["python3", "-m", "bluecore.mem.cli", "observe"]
        assert captured["env"] == {"X": "1"}
        assert captured["start_new_session"] is True
        assert written_stdin_content["text"] == "raw-payload"
        # 起動直後に unlink 済みで、ディレクトリにファイルが残らないこと。
        assert list((tmp_path / ".bluecore").glob("*.stdin")) == []

    def test_tempfile_creation_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)

        def fail_named_temp_file(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

        monkeypatch.setattr(hook_common.tempfile, "NamedTemporaryFile", fail_named_temp_file)

        assert detach_process(["true"], "raw") is False

    def test_popen_failure_returns_false_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)

        def fail_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("spawn failed")

        monkeypatch.setattr(hook_common.subprocess, "Popen", fail_popen)

        assert detach_process(["true"], "raw") is False
        assert list((tmp_path / ".bluecore").glob("*.stdin")) == []

    def test_unlink_failure_during_cleanup_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一時ファイルの unlink 失敗（既に削除済み等）でも起動成功を維持する。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)

        def fail_unlink(path):  # noqa: ANN001
            raise OSError("already removed")

        monkeypatch.setattr(hook_common.os, "unlink", fail_unlink)

        assert detach_process(["true"], "raw") is True

    def test_creates_bluecore_dir_as_0700_under_umask_022(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """umask 022 でも ~/.bluecore を 0700 で作る。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)
        old_umask = os.umask(0o022)
        try:
            assert detach_process(["true"], "raw") is True
            mode = stat.S_IMODE((tmp_path / ".bluecore").stat().st_mode)
            assert mode == 0o700
        finally:
            os.umask(old_umask)

    def test_tightens_existing_0755_bluecore_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既存 0755 の ~/.bluecore を 0700 に締め直す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("BLUECORE_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)
        bluecore = tmp_path / ".bluecore"
        bluecore.mkdir(mode=0o755)
        bluecore.chmod(0o755)
        assert detach_process(["true"], "raw") is True
        assert stat.S_IMODE(bluecore.stat().st_mode) == 0o700


def _pid_alive(pid: int) -> bool:
    """指定 PID のプロセスが生存しているかを判定する。

    Args:
        pid: 判定対象のプロセス ID。

    Returns:
        生存していれば True。

    Raises:
        例外は発生しません。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_dead(pid: int, deadline_seconds: float = 10.0) -> bool:
    """PID が消えるまでポーリングし、消えたかどうかを返す。

    Args:
        pid: 判定対象のプロセス ID。
        deadline_seconds: 待機する最大秒数。

    Returns:
        期限内にプロセスが消えたら True。

    Raises:
        例外は発生しません。
    """
    limit = time.monotonic() + deadline_seconds
    while time.monotonic() < limit:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestWatchdogKillsProcessGroup:
    """_WATCHDOG_SCRIPT が孫プロセスまで実プロセスで確実に殺すことのテスト。

    Popen.terminate()/kill() は直接の子 1 プロセスにしか届かず、子が起動した
    孫（desktop_notify の osascript / PowerShell 等）が残留する退行があった
    ため、argv 一致アサートではなく実際にプロセスを起動して wall-clock で
    検証する。
    """

    def _spawn_watchdog(
        self,
        tmp_path: Path,
        *,
        timeout: float,
        kill_after: float,
        ignore_sigterm: bool,
    ) -> tuple[subprocess.Popen, Path, int]:
        """watchdog → 子 → 孫の 3 段プロセスを起動し、孫の PID を返す。

        孫は `grandchild_sleep_seconds` 秒後にマーカーファイルを書くため、
        マーカーが存在しないことが「孫が仕事を完了する前に殺された」証跡になる。

        Args:
            tmp_path: マーカー / PID ファイルを置く一時ディレクトリ。
            timeout: watchdog が子へ SIGTERM を送るまでの秒数。
            kill_after: SIGTERM 後 SIGKILL へ昇格するまでの猶予秒数。
            ignore_sigterm: True なら子と孫が SIGTERM を無視する。

        Returns:
            (watchdog の Popen, マーカーパス, 孫の PID) のタプル。

        Raises:
            AssertionError: 孫の PID ファイルが期限内に作られない場合。
        """
        marker = tmp_path / "grandchild-marker"
        pid_file = tmp_path / "grandchild-pid"
        guard = "import signal;signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_sigterm else ""
        grandchild = f"{guard}import time;time.sleep(5);open({str(marker)!r},'w').write('alive')"
        child = (
            f"{guard}import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
            f"open({str(pid_file)!r},'w').write(str(p.pid));"
            "time.sleep(60)"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                hook_common._WATCHDOG_SCRIPT,
                str(timeout),
                str(kill_after),
                sys.executable,
                "-c",
                child,
            ]
        )
        limit = time.monotonic() + 10.0
        while time.monotonic() < limit and not pid_file.exists():
            time.sleep(0.02)
        assert pid_file.exists(), "孫プロセスが起動しなかった"
        return proc, marker, int(pid_file.read_text())

    def test_timeout_kills_grandchild(self, tmp_path: Path) -> None:
        """timeout 到達時、子だけでなく孫もプロセスグループごと殺される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=0.3, kill_after=0.3, ignore_sigterm=False
        )
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "孫プロセスが生存し続けた"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"

    def test_sigterm_ignoring_grandchild_is_escalated_to_sigkill(self, tmp_path: Path) -> None:
        """SIGTERM を無視する孫も kill_after 経過後 SIGKILL で回収される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=0.3, kill_after=0.3, ignore_sigterm=True
        )
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "SIGTERM を無視する孫が生存し続けた"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"

    def test_watchdog_sigterm_cascades_to_grandchild(self, tmp_path: Path) -> None:
        """watchdog 自身が SIGTERM を受けたとき、孫まで cascade して殺される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=60, kill_after=0.3, ignore_sigterm=False
        )
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "watchdog 終了後に孫が残留した"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"
