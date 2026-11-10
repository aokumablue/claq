"""detach 起動（``--bg`` の子プロセス起動・stdin 孤児の回収・bg ログ末尾の通知）。

フックを同期ブロックしないよう子を新しいセッションで起動する `detach_process` と、
その stdin 一時ファイルの回収、前回セッションの失敗痕跡の通知を提供する。
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from claq.lib.core_utils import ensure_private_dir, get_claq_dir, read_tail_text

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
log_file = open(os.environ["CLAQ_BG_LOG_PATH"], "ab")
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


def _bg_log_path(day: datetime) -> Path:
    """``day`` の日付の bg ログ（`~/.claq/logs/bg-YYYY-MM-DD.log`）の Path を返す。

    書き手の `_detach_log_path` と読み手の `recent_bg_failure_notice` が同じ名前を引くための
    単一情報源。名前がずれると、失敗の痕跡が残っていても通知されなくなる。

    Args:
        day: ログの日付。

    Returns:
        ログファイルの Path（存在は確かめない）。

    Raises:
        OSError: ホームディレクトリを決められない場合（``get_claq_dir`` 由来）。
    """
    return get_claq_dir() / "logs" / f"bg-{day:%Y-%m-%d}.log"


def _detach_log_path() -> Path:
    """detach した子プロセスの stdout/stderr を追記する、当日のログファイルのパスを返す。

    DEVNULL に捨てると、起動受付成功と処理成功の区別が付かなくなる。日付ごとに
    1 ファイルへ追記する。

    Returns:
        `~/.claq/logs/bg-YYYY-MM-DD.log` の Path。

    Raises:
        OSError: ログディレクトリを作れない場合（detach の起動を失敗にする）。
    """
    log_path = _bg_log_path(datetime.now())
    ensure_private_dir(log_path.parent)
    return log_path


# detach 用 stdin 一時ファイルの拡張子。
_DETACH_STDIN_SUFFIX = ".stdin"

# この秒数より古い ``*.stdin`` 孤児だけを回収する（実行中の detach を奪わない）。
# detach の実行上限は DETACH_TIMEOUT_SECONDS（600 秒）なので、1 時間は十分な余裕。
_DETACH_STDIN_GRACE_SECONDS = 60 * 60


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


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str]) -> None:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    起動前に `gc_detach_stdin_orphans` で過去の孤児を掃除する。一時ファイルは
    world-writable な /tmp を避けて ~/.claq 配下に作り、close→reopen の TOCTOU 窓を
    作らないよう同一 fd を seek(0) して子へ継承し、起動直後に unlink する。

    子は `_WATCHDOG_SCRIPT` でラップして DETACH_TIMEOUT_SECONDS で停止させる。
    子の stdout/stderr は `CLAQ_BG_LOG_PATH` で渡したログへ追記させる。戻ることは
    起動受付の成功を表し、子の処理結果ではない。

    Args:
        cmd: subprocess に渡すコマンドリスト。
        raw_stdin: 子プロセスへ渡す stdin の内容。
        env: 子プロセスの環境変数。

    Raises:
        OSError: 一時ファイル・ログディレクトリを作れない、または起動できない場合。
    """
    # 遅延 import: tempfile は shutil 経由で圧縮モジュール群を引き込む。SessionStart は
    # 起動のたびに本モジュールを import するが、一時ファイルが要るのは --bg で起動するときだけ。
    import tempfile

    gc_detach_stdin_orphans()
    private_dir = ensure_private_dir(get_claq_dir())
    stdin_file = tempfile.NamedTemporaryFile(
        mode="w+", encoding="utf-8", suffix=_DETACH_STDIN_SUFFIX, dir=private_dir, delete=False
    )
    try:
        stdin_file.write(raw_stdin)
        stdin_file.flush()
        stdin_file.seek(0)
        child_env = dict(env)
        child_env["CLAQ_BG_LOG_PATH"] = str(_detach_log_path())
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                _WATCHDOG_SCRIPT,
                str(DETACH_TIMEOUT_SECONDS),
                str(_DETACH_KILL_AFTER_SECONDS),
                *cmd,
            ],
            stdin=stdin_file,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=child_env,
            **detached_spawn_kwargs(),
        )
    finally:
        stdin_file.close()
        with contextlib.suppress(OSError):
            os.unlink(stdin_file.name)


# 前回セッション detach 失敗通知の 1 ファイルあたり読み取り上限バイト数。
# 1 行提示できれば十分なため小さく取る。
_BG_FAILURE_TAIL_MAX_BYTES = 4096


def recent_bg_failure_notice() -> str:
    """前回セッションの detach 起動（``--bg``）に失敗の痕跡があれば 1 行の通知を返す。

    detach した子の失敗は呼び出し元へ同期的に伝わらないため、次回 SessionStart で
    `_detach_log_path` のログ（正常系では空）に内容があれば末尾 1 行だけ提示する。
    SessionStart の timeout に対する境界として、当日＋前日の 2 ファイルを各末尾
    `_BG_FAILURE_TAIL_MAX_BYTES` バイトまでしか読まない。

    Returns:
        失敗の痕跡があれば人間可読の 1 行。無ければ空文字列。

    Raises:
        OSError: ホームディレクトリを決められない場合（``get_claq_dir`` 由来）。ログの読み取り失敗は
            そのファイルを飛ばすだけで送出しない。
    """
    today = datetime.now()
    for offset in (0, 1):
        path = _bg_log_path(today - timedelta(days=offset))
        if not path.is_file():
            continue
        try:
            tail = read_tail_text(path, _BG_FAILURE_TAIL_MAX_BYTES).strip()
        except OSError:
            continue
        if tail:
            return f"前回のバックグラウンド起動でエラーの痕跡があります（{path.name}）: {tail.splitlines()[-1]}"
    return ""
