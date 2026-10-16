"""detach 起動（``--bg`` の子プロセス起動・stdin 孤児の回収・bg ログ末尾の通知）のテスト。"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from claq.hooks import detach
from claq.hooks.detach import detach_process


class TestDetachLogPath:
    """_detach_log_path のテスト。"""

    def test_returns_dated_path_under_claq_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        path = detach._detach_log_path()

        assert path is not None
        assert path.parent == tmp_path / ".claq" / "logs"
        assert path.name.startswith("bg-") and path.name.endswith(".log")

    def test_returns_none_when_log_dir_cannot_be_created(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ログディレクトリを作れない場合は None（呼び出し元は DEVNULL へ倒す）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        (tmp_path / ".claq").mkdir()
        (tmp_path / ".claq" / "logs").write_text("not a directory", encoding="utf-8")

        assert detach._detach_log_path() is None


class TestReadTailBytes:
    """_read_tail_bytes のテスト。"""

    def test_reads_full_content_when_within_limit(self, tmp_path: Path) -> None:
        target = tmp_path / "small.log"
        target.write_text("hello\n", encoding="utf-8")
        assert detach._read_tail_bytes(target, 4096) == "hello\n"

    def test_reads_only_tail_when_exceeding_limit(self, tmp_path: Path) -> None:
        target = tmp_path / "big.log"
        target.write_bytes(b"A" * 100 + b"TAIL")
        assert detach._read_tail_bytes(target, 4) == "TAIL"

    def test_missing_file_returns_empty_string(self, tmp_path: Path) -> None:
        assert detach._read_tail_bytes(tmp_path / "missing.log", 4096) == ""


class TestRecentBgFailureNotice:
    """recent_bg_failure_notice のテスト。"""

    def _log_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        log_dir = tmp_path / ".claq" / "logs"
        log_dir.mkdir(parents=True)
        return log_dir

    def test_no_log_dir_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        assert detach.recent_bg_failure_notice() == ""

    def test_empty_log_file_returns_empty_string(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """正常系（対象プロセスが何も出力しない）はログが空になり通知なし。"""
        log_dir = self._log_dir(monkeypatch, tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        (log_dir / f"bg-{today}.log").write_text("", encoding="utf-8")
        assert detach.recent_bg_failure_notice() == ""

    def test_todays_log_with_content_returns_one_line_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        (log_dir / f"bg-{today}.log").write_text(
            "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n",
            encoding="utf-8",
        )

        notice = detach.recent_bg_failure_notice()

        assert notice != ""
        assert notice.count("\n") == 0
        assert "ModuleNotFoundError" in notice
        assert f"bg-{today}.log" in notice

    def test_falls_back_to_yesterdays_log_when_today_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        (log_dir / f"bg-{yesterday}.log").write_text("boom\n", encoding="utf-8")

        notice = detach.recent_bg_failure_notice()

        assert "boom" in notice
        assert f"bg-{yesterday}.log" in notice

    def test_ignores_logs_older_than_two_days(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        old = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        (log_dir / f"bg-{old}.log").write_text("ancient failure\n", encoding="utf-8")

        assert detach.recent_bg_failure_notice() == ""


class TestGcDetachStdinOrphans:
    """`gc_detach_stdin_orphans`（detach 用 stdin 一時ファイルの回収）のテスト。

    POSIX では起動直後に unlink されるため孤児は生じないが、Windows は開いて
    いるファイルを削除できず SessionEnd ごとに 1 個ずつ積み上がる（W4）。
    回収は生成側と同じ `get_claq_dir()` 基準で行う。
    """

    @staticmethod
    def _claq_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        """HOME を一時ディレクトリへ向け、その下の ``~/.claq`` を作って返す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        claq_dir = tmp_path / ".claq"
        claq_dir.mkdir()
        return claq_dir

    def test_stale_orphan_is_removed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """猶予を過ぎた `*.stdin` 孤児を回収すること。"""
        claq_dir = self._claq_dir(monkeypatch, tmp_path)
        stale = claq_dir / "tmpstale.stdin"
        stale.write_text("payload", encoding="utf-8")
        old = time.time() - detach._DETACH_STDIN_GRACE_SECONDS - 60
        os.utime(stale, (old, old))

        detach.gc_detach_stdin_orphans()

        assert not stale.exists()

    def test_fresh_orphan_and_other_files_survive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """実行中の detach が握るファイルと、拡張子が違うファイルは残すこと。"""
        claq_dir = self._claq_dir(monkeypatch, tmp_path)
        fresh = claq_dir / "tmpfresh.stdin"
        other = claq_dir / "mem.db"
        fresh.write_text("payload", encoding="utf-8")
        other.write_text("db", encoding="utf-8")
        old = time.time() - detach._DETACH_STDIN_GRACE_SECONDS - 60
        os.utime(other, (old, old))

        detach.gc_detach_stdin_orphans()

        assert fresh.exists()
        assert other.exists()

    def test_missing_directory_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ディレクトリが無くても例外を送出しないこと。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        detach.gc_detach_stdin_orphans()

    def test_stat_failure_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """個々のエントリの stat 失敗は無視して走査を続けること。"""
        claq_dir = self._claq_dir(monkeypatch, tmp_path)
        entry = claq_dir / "tmpfail.stdin"
        entry.write_text("payload", encoding="utf-8")
        real_stat = Path.stat

        def _boom(self: Path, *args: object, **kwargs: object) -> object:
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom)

        detach.gc_detach_stdin_orphans()

        assert entry.exists()


class TestDetachedSpawnKwargs:
    """`detached_spawn_kwargs`（プロセスグループ分離）のプラットフォーム契約。

    `start_new_session=True` は Windows で `ValueError` になり、SessionEnd の
    handoff hook がそこで落ちる。プラットフォーム名ではなく
    `os.setsid` の有無という capability で分岐することを固定する。
    """

    def test_posix_uses_new_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`os.setsid` がある環境では start_new_session を使うこと。"""
        monkeypatch.setattr(os, "setsid", lambda: None, raising=False)

        assert detach.detached_spawn_kwargs() == {"start_new_session": True}

    def test_windows_uses_creation_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`os.setsid` が無い環境では creationflags でプロセスグループを分けること。"""
        monkeypatch.delattr(os, "setsid", raising=False)
        monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

        assert detach.detached_spawn_kwargs() == {"creationflags": 0x208}


class TestDetachProcess:
    """detach_process の一時ファイル経由 stdin 引き渡し・エラー処理のテスト。

    呼び出し元は launcher.py の `--bg` 実行 1 箇所のみ（`_run_background`）。
    """

    def test_launches_detached_process_and_cleans_up_tmp_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        captured = {}
        written_stdin_content = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["cmd"] = cmd
                captured["env"] = env
                captured["start_new_session"] = start_new_session
                stdin.seek(0)
                written_stdin_content["text"] = stdin.read()

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        result = detach_process(["python3", "-m", "claq.mem.cli", "observe"], "raw-payload", env={"X": "1"})

        assert result is True
        assert captured["cmd"][:3] == [sys.executable, "-c", detach._WATCHDOG_SCRIPT]
        assert captured["cmd"][3:5] == [
            str(detach.DETACH_TIMEOUT_SECONDS),
            str(detach._DETACH_KILL_AFTER_SECONDS),
        ]
        assert captured["cmd"][5:] == ["python3", "-m", "claq.mem.cli", "observe"]
        assert captured["env"]["X"] == "1"
        # 呼び出し元の env はそのまま維持しつつ、対象プロセスの stdout/stderr を
        # DEVNULL の代わりに追記させるログパスが差し込まれること。
        log_path = Path(captured["env"]["CLAQ_BG_LOG_PATH"])
        assert log_path.parent == tmp_path / ".claq" / "logs"
        assert log_path.name.startswith("bg-") and log_path.name.endswith(".log")
        assert captured["start_new_session"] is True
        assert written_stdin_content["text"] == "raw-payload"
        # 起動直後に unlink 済みで、ディレクトリにファイルが残らないこと。
        assert list((tmp_path / ".claq").glob("*.stdin")) == []

    def test_env_none_still_gets_log_path_injected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """env=None（親環境継承）でもログパスは差し込まれること。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["env"] = env
                stdin.seek(0)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)

        assert detach_process(["true"], "raw") is True
        assert "CLAQ_BG_LOG_PATH" in captured["env"]

    def test_log_dir_creation_failure_falls_back_to_devnull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ログディレクトリを作れなくても detach 自体は継続する（best-effort）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["env"] = env
                stdin.seek(0)

        monkeypatch.setattr(subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(detach, "_detach_log_path", lambda: None)

        assert detach_process(["true"], "raw", env={"X": "1"}) is True
        assert captured["env"] == {"X": "1"}
        assert "CLAQ_BG_LOG_PATH" not in captured["env"]

    def test_tempfile_creation_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        def fail_named_temp_file(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", fail_named_temp_file)

        assert detach_process(["true"], "raw") is False

    def test_popen_failure_returns_false_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)

        def fail_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("spawn failed")

        monkeypatch.setattr(subprocess, "Popen", fail_popen)

        assert detach_process(["true"], "raw") is False
        assert list((tmp_path / ".claq").glob("*.stdin")) == []

    def test_unlink_failure_during_cleanup_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一時ファイルの unlink 失敗（既に削除済み等）でも起動成功を維持する。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)

        def fail_unlink(path):  # noqa: ANN001
            raise OSError("already removed")

        monkeypatch.setattr(os, "unlink", fail_unlink)

        assert detach_process(["true"], "raw") is True

    def test_creates_claq_dir_as_0700_under_umask_022(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """umask 022 でも ~/.claq を 0700 で作る。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
        old_umask = os.umask(0o022)
        try:
            assert detach_process(["true"], "raw") is True
            mode = stat.S_IMODE((tmp_path / ".claq").stat().st_mode)
            assert mode == 0o700
        finally:
            os.umask(old_umask)

    def test_tightens_existing_0755_claq_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既存 0755 の ~/.claq を 0700 に締め直す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: None)
        claq = tmp_path / ".claq"
        claq.mkdir(mode=0o755)
        claq.chmod(0o755)
        assert detach_process(["true"], "raw") is True
        assert stat.S_IMODE(claq.stat().st_mode) == 0o700


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
                detach._WATCHDOG_SCRIPT,
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
