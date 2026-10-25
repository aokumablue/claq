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


@pytest.fixture(autouse=True)
def _home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """HOME を tmp_path へ向けて CLAQ_HOME を外し、`~/.claq` を tmp_path 配下に閉じる。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAQ_HOME", raising=False)


@pytest.mark.parametrize(
    ("content", "max_bytes", "expected"),
    [(b"hello\n", 4096, "hello\n"), (b"A" * 100 + b"TAIL", 4, "TAIL"), (None, 4096, "")],
    ids=["within-limit", "tail-only", "missing-file"],
)
def test_read_tail_bytes(tmp_path: Path, content: bytes | None, max_bytes: int, expected: str) -> None:
    """上限以内なら全体、超えれば末尾の上限バイトだけを返し、読めないファイルは空文字列になること。"""
    target = tmp_path / "bg.log"
    if content is not None:
        target.write_bytes(content)

    assert detach._read_tail_bytes(target, max_bytes) == expected


@pytest.mark.parametrize(
    ("logs", "expected"),
    [
        pytest.param(None, None, id="no-log-dir"),
        # 正常系（対象プロセスが何も出力しない）はログが空になり通知しない。
        pytest.param({0: ""}, None, id="empty-today"),
        pytest.param(
            {0: "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n"},
            (0, "ModuleNotFoundError: No module named 'x'"),
            id="today-last-line",
        ),
        pytest.param({1: "boom\n"}, (1, "boom"), id="yesterday-when-today-missing"),
        pytest.param({2: "ancient failure\n"}, None, id="older-than-two-days"),
    ],
)
def test_recent_bg_failure_notice(
    tmp_path: Path, logs: dict[int, str] | None, expected: tuple[int, str] | None
) -> None:
    """当日→前日の順にログの末尾 1 行をファイル名つきの 1 行で通知し、空・2 日前・ディレクトリ無しは通知しないこと。"""
    names = {offset: f"bg-{datetime.now() - timedelta(days=offset):%Y-%m-%d}.log" for offset in range(3)}
    if logs is not None:
        log_dir = tmp_path / ".claq" / "logs"
        log_dir.mkdir(parents=True)
        for offset, text in logs.items():
            (log_dir / names[offset]).write_text(text, encoding="utf-8")

    notice = detach.recent_bg_failure_notice()

    if expected is None:
        assert notice == ""
    else:
        offset, last_line = expected
        assert notice == f"前回のバックグラウンド起動でエラーの痕跡があります（{names[offset]}）: {last_line}"


@pytest.mark.parametrize(
    ("name", "stale", "stat_fails", "survives"),
    [
        ("tmpstale.stdin", True, False, False),
        # 実行中の detach が握る新しい孤児と、拡張子が違うファイル（古くても）は残す。
        ("tmpfresh.stdin", False, False, True),
        ("mem.db", True, False, True),
        # エントリの stat 失敗は無視して走査を続ける（古くても消さない）。
        ("tmpfail.stdin", True, True, True),
    ],
    ids=["stale-orphan-removed", "fresh-orphan-kept", "other-suffix-kept", "stat-failure-ignored"],
)
def test_gc_detach_stdin_orphans(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str, stale: bool, stat_fails: bool, survives: bool
) -> None:
    """猶予を過ぎた `*.stdin` 孤児だけを回収すること（Windows は開いたファイルを消せず SessionEnd ごとに積み上がる）。"""
    entry = tmp_path / ".claq" / name
    entry.parent.mkdir()
    entry.write_text("payload", encoding="utf-8")
    if stale:
        old = time.time() - detach._DETACH_STDIN_GRACE_SECONDS - 60
        os.utime(entry, (old, old))
    if stat_fails:
        real_stat = Path.stat

        def _stat(self: Path, *args: object, **kwargs: object) -> object:
            """対象エントリの stat だけを失敗させる。"""
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _stat)

    detach.gc_detach_stdin_orphans()

    assert os.path.exists(entry) is survives


def test_gc_without_claq_dir_is_noop() -> None:
    """`~/.claq` が無くても例外を送出しないこと。"""
    detach.gc_detach_stdin_orphans()


@pytest.mark.parametrize(
    ("has_setsid", "expected"),
    [(True, {"start_new_session": True}), (False, {"creationflags": 0x208})],
    ids=["posix-new-session", "windows-creation-flags"],
)
def test_detached_spawn_kwargs(monkeypatch: pytest.MonkeyPatch, has_setsid: bool, expected: dict) -> None:
    """プラットフォーム名ではなく `os.setsid` の有無で分岐すること（start_new_session は Windows で ValueError になる）。"""
    if has_setsid:
        monkeypatch.setattr(os, "setsid", lambda: None, raising=False)
    else:
        monkeypatch.delattr(os, "setsid", raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)

    assert detach.detached_spawn_kwargs() == expected


@pytest.fixture
def popen_calls(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """subprocess.Popen を記録係に替え、呼び出しごとの argv・キーワード引数・継承した stdin の中身を返す。"""
    calls: list[dict] = []

    def _record(cmd: list[str], **kwargs: object) -> None:
        """呼び出しを記録し、子へ継承される stdin を先頭から読む。"""
        kwargs["stdin"].seek(0)
        calls.append({"cmd": cmd, "stdin_text": kwargs["stdin"].read(), **kwargs})

    monkeypatch.setattr(subprocess, "Popen", _record)
    return calls


@pytest.mark.parametrize(
    ("env", "logs_blocked"),
    [({"X": "1"}, False), (None, False), ({"X": "1"}, True)],
    ids=["env-given", "env-inherited", "log-dir-unavailable"],
)
def test_detach_process_launches_watchdog(
    tmp_path: Path, popen_calls: list[dict], env: dict[str, str] | None, logs_blocked: bool
) -> None:
    """watchdog 越しに起動して stdin を一時ファイルで渡し、子の env へ bg ログのパスを足すこと（ログを作れなければ足さずに起動する）。"""
    if logs_blocked:
        (tmp_path / ".claq").mkdir()
        (tmp_path / ".claq" / "logs").write_text("not a directory", encoding="utf-8")

    assert detach_process(["python3", "-m", "claq.mem.cli", "observe"], "raw-payload", env=env) is True

    [call] = popen_calls
    assert call["cmd"] == [
        sys.executable,
        "-c",
        detach._WATCHDOG_SCRIPT,
        str(detach.DETACH_TIMEOUT_SECONDS),
        str(detach._DETACH_KILL_AFTER_SECONDS),
        "python3",
        "-m",
        "claq.mem.cli",
        "observe",
    ]
    assert call["stdin_text"] == "raw-payload"
    assert call["start_new_session"] is True
    child_env = dict(call["env"])
    log_path = child_env.pop("CLAQ_BG_LOG_PATH", None)
    assert child_env == (dict(os.environ) if env is None else env)
    if logs_blocked:
        assert log_path is None
    else:
        assert Path(log_path).parent == tmp_path / ".claq" / "logs"
        assert Path(log_path).name.startswith("bg-") and log_path.endswith(".log")
    # 起動直後に unlink 済みで、一時ファイルが残らないこと。
    assert list((tmp_path / ".claq").glob("*.stdin")) == []


def _raise_oserror(*args: object, **kwargs: object) -> None:
    """注入した OSError を送出する。"""
    raise OSError("injected")


@pytest.mark.parametrize(
    ("target", "name", "expected", "leftovers"),
    [(tempfile, "NamedTemporaryFile", False, 0), (subprocess, "Popen", False, 0), (os, "unlink", True, 1)],
    ids=["tempfile-fails", "popen-fails", "unlink-fails"],
)
@pytest.mark.usefixtures("popen_calls")
def test_detach_process_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, target: object, name: str, expected: bool, leftovers: int
) -> None:
    """一時ファイル作成・起動の失敗は False（一時ファイルは消す）、起動後の unlink 失敗は起動成功の True のままであること。"""
    monkeypatch.setattr(target, name, _raise_oserror)

    assert detach_process(["true"], "raw") is expected
    assert len(list((tmp_path / ".claq").glob("*.stdin"))) == leftovers


@pytest.mark.parametrize("existing_mode", [None, 0o755], ids=["created-under-umask-022", "existing-0755-tightened"])
@pytest.mark.usefixtures("popen_calls")
def test_detach_process_keeps_claq_dir_private(tmp_path: Path, existing_mode: int | None) -> None:
    """umask 022 で作るときも既存 0755 のときも、`~/.claq` を 0700 にすること。"""
    claq = tmp_path / ".claq"
    old_umask = os.umask(0o022)
    try:
        if existing_mode is not None:
            claq.mkdir()
            claq.chmod(existing_mode)
        assert detach_process(["true"], "raw") is True
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(claq.stat().st_mode) == 0o700


def _wait_until_dead(pid: int, deadline_seconds: float = 10.0) -> bool:
    """PID が消えるまでポーリングし、期限内に消えたかを返す。"""
    limit = time.monotonic() + deadline_seconds
    while time.monotonic() < limit:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize(
    ("timeout", "ignore_sigterm", "signal_watchdog"),
    [(0.3, False, False), (0.3, True, False), (60, False, True)],
    ids=["timeout-kills-grandchild", "sigterm-ignoring-escalated-to-sigkill", "watchdog-sigterm-cascades"],
)
def test_watchdog_kills_process_group(
    tmp_path: Path, timeout: float, ignore_sigterm: bool, signal_watchdog: bool
) -> None:
    """timeout・SIGTERM を無視する子孫・watchdog 自身への SIGTERM のいずれでも、孫が仕事を終える前に殺すこと。

    Popen.terminate()/kill() は直接の子にしか届かず、孫（desktop_notify の osascript 等）が残留する退行が
    あったため、argv の一致ではなく watchdog → 子 → 孫の実プロセスを起動して確かめる。
    """
    marker = tmp_path / "grandchild-marker"
    pid_file = tmp_path / "grandchild-pid"
    guard = "import signal;signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_sigterm else ""
    # 孫は 5 秒後にマーカーを書く。マーカーが無いことが「仕事を終える前に殺された」証跡になる。
    grandchild = f"{guard}import time;time.sleep(5);open({str(marker)!r},'w').write('alive')"
    child = (
        f"{guard}import subprocess,sys,time;"
        f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
        f"open({str(pid_file)!r},'w').write(str(p.pid));"
        "time.sleep(60)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", detach._WATCHDOG_SCRIPT, str(timeout), "0.3", sys.executable, "-c", child]
    )
    limit = time.monotonic() + 10.0
    while time.monotonic() < limit and not (pid_file.exists() and pid_file.read_text()):
        time.sleep(0.02)
    assert pid_file.exists() and pid_file.read_text(), "孫プロセスが起動しなかった"
    grandchild_pid = int(pid_file.read_text())

    if signal_watchdog:
        proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)

    assert _wait_until_dead(grandchild_pid), "孫プロセスが生存し続けた"
    assert not marker.exists(), "孫プロセスが仕事を完了してしまった"
