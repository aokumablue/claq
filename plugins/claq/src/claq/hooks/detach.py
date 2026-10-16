"""detach 起動（``--bg`` の子プロセス起動・stdin 孤児の回収・bg ログ末尾の通知）。

フックを同期ブロックしないよう子を新しいセッションで起動する `detach_process` と、
その stdin 一時ファイルの回収、前回セッションの失敗痕跡の通知を提供する。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from claq.lib.core_utils import ensure_private_dir, get_claq_dir

# detach 起動した子の実行時間上限（秒）。detach 後の子はハーネスの timeout の
# 管轄外なので、自前の watchdog で止める。正常系はローカル I/O 数秒で終わるため、
# 正常系を絶対に切らず、確実に異常と断定できる 10 分を採る。
DETACH_TIMEOUT_SECONDS = 600
# SIGTERM を無視して詰まったプロセスを SIGKILL で確実に回収するまでの猶予（秒）。
_DETACH_KILL_AFTER_SECONDS = 30

# detach した子を DETACH_TIMEOUT_SECONDS で穏当に停止し、応答が無ければ
# _DETACH_KILL_AFTER_SECONDS 後に強制終了する watchdog。coreutils の `timeout` は
# BSD/macOS に標準で無く、ランタイム依存ゼロの方針にも反するため sys.executable で書く。
#
# POSIX では子を新しいプロセスグループのリーダーにし、os.killpg で孫までまとめて
# 回収する（Popen.terminate()/kill() は直接の子にしか届かない）。Windows には killpg /
# SIGKILL が無いため、capability（`hasattr(os, "killpg")`）で分岐して
# CREATE_NEW_PROCESS_GROUP|DETACHED_PROCESS で起動し Popen.terminate()/kill() で回収する。
# Windows で孫が回収されない差は受容する（現在の対象の孫は `git` だけで、git 側にも
# ハードタイムアウトがある）。
#
# watchdog 自身が SIGTERM を受けた場合も孫を残さないよう、ハンドラで子へ停止を
# cascade する（ハンドラ内で proc.wait() を再入させないよう time.sleep で待つ）。
#
# detach 1 回につき 2 プロセスが起動するが、watchdog を外すとハングした子と孫を
# 回収する主体が消える。子プロセス内の `signal.alarm` は孫に届かず Windows に無いため
# 代替にならない。
_WATCHDOG_SCRIPT = """
import os, signal, subprocess, sys, time

timeout, kill_after = float(sys.argv[1]), float(sys.argv[2])
log_path = os.environ.get("CLAQ_BG_LOG_PATH")
log_file = open(log_path, "ab") if log_path else subprocess.DEVNULL
has_process_groups = hasattr(os, "killpg")
if has_process_groups:
    spawn_kwargs = {"start_new_session": True}
else:
    spawn_kwargs = {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    }
proc = subprocess.Popen(
    sys.argv[3:],
    stdin=sys.stdin,
    stdout=log_file,
    stderr=log_file,
    **spawn_kwargs,
)

def stop_child(hard):
    try:
        if has_process_groups:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL if hard else signal.SIGTERM)
        elif hard:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, OSError):
        pass

def cascade(signum, frame):
    stop_child(False)
    time.sleep(kill_after)
    stop_child(True)
    os._exit(128 + signum)

signal.signal(signal.SIGTERM, cascade)
try:
    proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    stop_child(False)
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        stop_child(True)
        proc.wait()
"""


def detached_spawn_kwargs() -> dict[str, Any]:
    """親から独立したプロセスグループで子を起動するための Popen 引数を返す。

    POSIX は ``start_new_session=True``、Windows は
    ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS``（``start_new_session`` は Windows で
    ``ValueError`` になる）。分岐は ``os.setsid`` の有無という capability で行う。

    Returns:
        ``subprocess.Popen`` へ展開する追加キーワード引数。

    Raises:
        例外は発生しません。
    """
    if hasattr(os, "setsid"):
        return {"start_new_session": True}
    return {
        "creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    }


def _detach_log_path() -> Path | None:
    """detach した子プロセスの stdout/stderr を追記するログファイルのパスを返す。

    DEVNULL に捨てると、起動受付成功と処理成功の区別が付かなくなる。日付ごとに
    1 ファイルへ追記する。best-effort の diagnostics であり、ログディレクトリを
    作れない場合も detach 自体は継続させる。

    Returns:
        `~/.claq/logs/bg-YYYY-MM-DD.log` の Path。作成失敗時は None。

    Raises:
        例外は発生しません。
    """
    try:
        log_dir = ensure_private_dir(get_claq_dir() / "logs")
    except OSError:
        return None
    return log_dir / f"bg-{datetime.now():%Y-%m-%d}.log"


_DETACH_STDIN_SUFFIX = ".stdin"
"""detach 用 stdin 一時ファイルの拡張子。"""

_DETACH_STDIN_GRACE_SECONDS = 60 * 60
"""この秒数より古い ``*.stdin`` 孤児だけを回収する（実行中の detach を奪わない）。
detach の実行上限は DETACH_TIMEOUT_SECONDS（600 秒）なので、1 時間は十分な余裕。"""


def gc_detach_stdin_orphans() -> None:
    """``~/.claq`` 直下に残った detach 用 stdin 一時ファイルを age-gate で回収する。

    POSIX では `detach_process` が起動直後に unlink するため孤児は生じない。Windows は
    子が継承ハンドルを保持していて unlink が必ず失敗し、起動ごとに積み上がる。
    走査先を生成側と同じ ``get_claq_dir()``（``CLAQ_HOME`` を見る）に揃えるため、
    生成側と同じモジュールに置く。

    Raises:
        例外は発生しません（iterdir/stat/unlink の失敗は無視します）。
    """
    cutoff = time.time() - _DETACH_STDIN_GRACE_SECONDS
    try:
        entries = list(get_claq_dir().iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.endswith(_DETACH_STDIN_SUFFIX):
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            continue


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str] | None = None) -> bool:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    起動前に `gc_detach_stdin_orphans` で過去の孤児を掃除する。一時ファイルは
    world-writable な /tmp を避けて ~/.claq 配下に作り、close→reopen の TOCTOU 窓を
    作らないよう同一 fd を seek(0) して子へ継承し、起動直後に unlink する。

    子は `_WATCHDOG_SCRIPT` でラップして DETACH_TIMEOUT_SECONDS で停止させる。
    子の stdout/stderr は `CLAQ_BG_LOG_PATH` で渡したログへ追記させる（ログを
    作れなければ DEVNULL）。戻り値は起動受付の成否であり、子の処理結果ではない。

    Args:
        cmd: subprocess に渡すコマンドリスト。
        raw_stdin: 子プロセスへ渡す stdin の内容。
        env: 子プロセスの環境変数。None なら親の環境を継承する。

    Returns:
        起動に成功した場合 True、OSError 時は False。

    Raises:
        例外は発生しません。
    """
    gc_detach_stdin_orphans()
    try:
        private_dir = ensure_private_dir(get_claq_dir())
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", suffix=_DETACH_STDIN_SUFFIX, dir=private_dir, delete=False
        )
    except OSError:
        return False
    try:
        tmp.write(raw_stdin)
        tmp.flush()
        tmp.seek(0)
        child_env = dict(os.environ if env is None else env)
        log_path = _detach_log_path()
        if log_path is not None:
            child_env["CLAQ_BG_LOG_PATH"] = str(log_path)
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WATCHDOG_SCRIPT,
                str(DETACH_TIMEOUT_SECONDS),
                str(_DETACH_KILL_AFTER_SECONDS),
                *cmd,
            ],
            stdin=tmp,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            **detached_spawn_kwargs(),
        )
        return True
    except OSError:
        return False
    finally:
        tmp.close()
        with contextlib.suppress(OSError):
            os.unlink(tmp.name)


# 前回セッション detach 失敗通知の 1 ファイルあたり読み取り上限バイト数。
# 1 行提示できれば十分なため小さく取る。
_BG_FAILURE_TAIL_MAX_BYTES = 4096


def _read_tail_bytes(path: Path, max_bytes: int) -> str:
    """ファイル末尾を上限バイトまで読む（seek-from-end）。

    Args:
        path: 読み取り対象のファイル。
        max_bytes: 末尾から読む最大バイト数。

    Returns:
        デコード済みの末尾テキスト。読めなければ空文字列。

    Raises:
        例外は発生しません。
    """
    try:
        with path.open("rb") as stream:
            stream.seek(0, 2)
            stream.seek(max(0, stream.tell() - max_bytes))
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def recent_bg_failure_notice() -> str:
    """前回セッションの detach 起動（``--bg``）に失敗の痕跡があれば 1 行の通知を返す。

    detach した子の失敗は呼び出し元へ同期的に伝わらないため、次回 SessionStart で
    `_detach_log_path` のログ（正常系では空）に内容があれば末尾 1 行だけ提示する。
    SessionStart の timeout に対する境界として、当日＋前日の 2 ファイルを各末尾
    `_BG_FAILURE_TAIL_MAX_BYTES` バイトまでしか読まない。

    Returns:
        失敗の痕跡があれば人間可読の 1 行。無ければ空文字列。

    Raises:
        例外は発生しません。
    """
    log_dir = get_claq_dir() / "logs"
    today = datetime.now()
    for offset in (0, 1):
        path = log_dir / f"bg-{today - timedelta(days=offset):%Y-%m-%d}.log"
        if not path.is_file():
            continue
        tail = _read_tail_bytes(path, _BG_FAILURE_TAIL_MAX_BYTES).strip()
        if tail:
            return f"前回のバックグラウンド起動でエラーの痕跡があります（{path.name}）: {tail.splitlines()[-1]}"
    return ""
