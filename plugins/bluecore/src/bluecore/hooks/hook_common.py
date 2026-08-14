"""bluecoreフック実装の共通ユーティリティ。

フック用の入力読み込み、JSON解析、
出力書き込みの共有関数を提供します。
"""

from __future__ import annotations

import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from bluecore.hooks.output_adapter import adapt_context_output, emit_block
from bluecore.lib.core_utils import ensure_private_dir, get_bluecore_dir

MAX_STDIN_BYTES = 1024 * 1024

# hooks は Claude Code が spawn 直後に stdin へ JSON を書き込むため、
# 最初のバイト到着まで 2 秒あれば十分な余裕がある。
# stdin リダイレクト漏れ（パイプ未接続のまま open）での無期限ブロックを防ぐ。
# launcher がインプロセス実行になったことで、この guard は各フックが
# 自分で stdin を読む read_raw_stdin* の先頭に置く（旧: launcher._read_stdin）。
STDIN_FIRST_BYTE_TIMEOUT = 2.0


def _stdin_ready() -> bool:
    """stdin が TTY でなく、最初のバイトが時間内に届くかを判定します。

    Args:
        なし

    Returns:
        読み取りを続行してよければ True。TTY 接続時、または
        STDIN_FIRST_BYTE_TIMEOUT 秒以内に最初のバイトが到着しない場合は
        False（後者は stderr に警告を出す）。

    Raises:
        例外は発生しません。
    """
    if sys.stdin.isatty():
        return False

    ready, _, _ = select.select([sys.stdin], [], [], STDIN_FIRST_BYTE_TIMEOUT)
    if not ready:
        write_stderr(
            "WARNING: stdin から入力が届かないため空入力で続行します（stdin リダイレクト漏れの可能性）\n"
        )
        return False
    return True


def _read_stdin_bytes(max_bytes: int) -> bytes:
    """stdin から最大 `max_bytes` 分をバイト列として読みます。

    `.buffer` がある場合はバイト単位で読みます。無い場合（io.StringIO 等）は
    文字数で読んだあと UTF-8 に再エンコードします。文字数 read ではバイト
    上限を最大 4 倍超過しうるため、呼び出し側でバイト換算の切り詰めを行います。

    Args:
        max_bytes: 読み取る最大バイト数（buffer 無し時は最大文字数）です。

    Returns:
        読み取ったバイト列を返します。

    Raises:
        例外は発生しません。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is not None:
        return stdin_buffer.read(max_bytes)
    return sys.stdin.read(max_bytes).encode("utf-8", errors="replace")


def read_raw_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """標準入力から生のテキストをバイト単位の上限つきで読み取ります。

    TTY 接続時、または最初のバイトが STDIN_FIRST_BYTE_TIMEOUT 秒以内に
    届かない場合は空文字列を返します（stdin リダイレクト漏れでの無期限
    ブロックを防ぐ）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列（max_bytes バイトで切り捨て済み）を返します。

    Raises:
        例外は発生しません。
    """
    if not _stdin_ready():
        return ""
    return _read_stdin_bytes(max_bytes)[:max_bytes].decode("utf-8", errors="replace")


def read_raw_stdin_with_truncation(max_bytes: int = MAX_STDIN_BYTES) -> tuple[str, bool]:
    """標準入力を読み取り、切り捨ての有無を返します。

    TTY 接続時、または最初のバイトが STDIN_FIRST_BYTE_TIMEOUT 秒以内に
    届かない場合は ("", False) を返します（stdin リダイレクト漏れでの
    無期限ブロックを防ぐ）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取った文字列と、切り捨てが発生したかどうかのタプルを返します。

    Raises:
        例外は発生しません。
    """
    if not _stdin_ready():
        return "", False
    raw_bytes = _read_stdin_bytes(max_bytes + 1)
    truncated = len(raw_bytes) > max_bytes
    if truncated:
        raw_bytes = raw_bytes[:max_bytes]
    return raw_bytes.decode("utf-8", errors="replace"), truncated


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """JSON 文字列を辞書としてパースします。

    Args:
        raw: パース対象の JSON 文字列です。

    Returns:
        パースされた辞書、または失敗時は None を返します。

    Raises:
        例外は発生せず、パースエラー時は None を返します。
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def write_stdout(text: str) -> None:
    """標準出力にテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stdout.write(text)


def write_stderr(text: str) -> None:
    """標準エラーにテキストを書き出します。

    Args:
        text: 出力するテキストです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    sys.stderr.write(text)


def is_truthy(value: str | None) -> bool:
    """文字列が真値を表すかどうかを判定します。

    Args:
        value: 判定対象の文字列です。

    Returns:
        '1', 'true', 'yes', 'on' の場合は True を返します。

    Raises:
        例外は発生しません。
    """
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def basename(path: str) -> str:
    """パスからファイル名を取得します。

    Args:
        path: ファイルパスです。

    Returns:
        ファイル名を返します。

    Raises:
        例外は発生しません。
    """
    return Path(path).name


# detach 起動した子の実行時間上限（秒）。start_new_session=True の子はハーネスの
# timeout で kill されないため、自前の watchdog で自決させる。
#
# この値は hooks.json の timeout とは無関係に決める。detach 後の子はハーネスの
# 管轄外であり、hooks.json の値（最大は mem.cli context の 60 秒だが、これは
# detach しない同期エントリ）と紐付ける論拠がないため。
#
# detach 対象（launcher --bg: learn.observe / session_end / desktop_notify /
# mem.cli handoff）はいずれも正常系ではローカル I/O 数秒で終わる。したがって
# 上限は「正常系を絶対に切らない」ことを優先した安全網の閾値であり、
# 10 分走り続けていれば確実に異常（ハング・暴走）と断定できる 600 秒を採る。
DETACH_TIMEOUT_SECONDS = 600
# SIGTERM を無視して詰まったプロセスを SIGKILL で確実に回収するまでの猶予（秒）。
_DETACH_KILL_AFTER_SECONDS = 30

# detach した子を DETACH_TIMEOUT_SECONDS で SIGTERM、応答なければ
# _DETACH_KILL_AFTER_SECONDS 後に SIGKILL する watchdog。coreutils の
# `timeout`/`gtimeout` は BSD/macOS に標準で存在せず（--kill-after は GNU 固有）、
# ランタイム依存ゼロの方針にも反するため、既に起動に使っている sys.executable
# 自身で実装し外部コマンドへの依存をなくす。
#
# シグナルは GNU timeout と同様に「プロセスグループ」へ送る。子を
# start_new_session=True で新しいセッション（= 新しいプロセスグループ）の
# リーダーにし、os.killpg で子と孫をまとめて回収する。Popen.terminate()/kill()
# は直接の子 1 プロセスにしか届かず、子が起動した孫（desktop_notify の
# osascript / PowerShell、quality_gate の lint ステップ）が無期限に残留する。
#
# watchdog 自身が SIGTERM を受けた場合も、そのまま終了すると孫が残るため、
# ハンドラで子グループへ SIGTERM を cascade し、猶予後に SIGKILL してから
# 抜ける（ハンドラ内で proc.wait() を再入させないよう time.sleep で待つ）。
#
# コスト: detach 1 回につき watchdog + 対象の 2 プロセスが起動する。hooks.json の
# `--bg bluecore.skills.learn.observe pre` は matcher "*" で全ツールコールに発火
# するため、非 Claude ハーネスではツールコールごとにこの 2 プロセスを払う。これは
# 意図的なコストであり、削減目的で watchdog を外してはならない:
#   - watchdog を消すと、detach 済みの子と孫を kill する主体が消滅する。子は
#     ハーネス timeout の管轄外なので、ハングした子と孫が無制限に残留する。
#   - 子プロセス内の `signal.alarm` では代替できない。alarm は自プロセスにしか
#     届かず、子が起動した孫（desktop_notify の osascript / PowerShell、
#     quality_gate の lint ステップ）を回収できないため等価ではない。
#   - watchdog は sys.executable の `-c` 実行で、対象モジュールを import せず
#     待つだけなので、追加コストは Python インタプリタ起動 1 回分に留まる。
# すなわち「毎回 1 プロセス分の起動コスト」と「孫プロセスの無制限残留を防ぐ
# kill 保証」のトレードオフであり、後者を採る。
_WATCHDOG_SCRIPT = """
import os, signal, subprocess, sys, time

timeout, kill_after = float(sys.argv[1]), float(sys.argv[2])
proc = subprocess.Popen(
    sys.argv[3:],
    stdin=sys.stdin,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
    start_new_session=True,
)

def signal_group(sig):
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, OSError):
        pass

def cascade(signum, frame):
    signal_group(signal.SIGTERM)
    time.sleep(kill_after)
    signal_group(signal.SIGKILL)
    os._exit(128 + signum)

signal.signal(signal.SIGTERM, cascade)
try:
    proc.wait(timeout=timeout)
except subprocess.TimeoutExpired:
    signal_group(signal.SIGTERM)
    try:
        proc.wait(timeout=kill_after)
    except subprocess.TimeoutExpired:
        signal_group(signal.SIGKILL)
        proc.wait()
"""


def _watchdog_argv(cmd: list[str]) -> list[str]:
    """detach 対象を watchdog 付きで起動する argv を組み立てます。

    Args:
        cmd: 監視対象のコマンドリストです。

    Returns:
        `sys.executable -c _WATCHDOG_SCRIPT` で対象を包んだ argv を返します。

    Raises:
        例外は発生しません。
    """
    return [
        sys.executable,
        "-c",
        _WATCHDOG_SCRIPT,
        str(DETACH_TIMEOUT_SECONDS),
        str(_DETACH_KILL_AFTER_SECONDS),
        *cmd,
    ]


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str] | None = None) -> bool:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    親プロセスの終了に影響されず子を走らせ続けるために使う。一時ファイルは
    world-writable な /tmp を避けて ~/.bluecore 配下に作成し、close→reopen の
    TOCTOU 窓を作らないよう同一 fd を seek(0) して子へ継承する。起動直後に
    unlink する（継承済み fd は有効なまま）。

    detach 後の子はハーネスの timeout の管轄外になるため、_WATCHDOG_SCRIPT で
    ラップして DETACH_TIMEOUT_SECONDS で SIGTERM、さらに猶予後 SIGKILL を送り、
    暴走プロセスの無期限残留を防ぐ。シグナルは子のプロセスグループへ送るため、
    子が起動した孫プロセスもまとめて回収される。

    Args:
        cmd: subprocess に渡すコマンドリスト。
        raw_stdin: 子プロセスへ渡す stdin の内容。
        env: 子プロセスの環境変数。None なら親の環境を継承する。

    Returns:
        起動に成功した場合 True、OSError 時は False。

    Raises:
        例外は発生しません。
    """
    try:
        private_dir = ensure_private_dir(get_bluecore_dir())
        tmp = tempfile.NamedTemporaryFile(
            mode="w+", encoding="utf-8", suffix=".stdin", dir=private_dir, delete=False
        )
    except OSError:
        return False
    try:
        tmp.write(raw_stdin)
        tmp.flush()
        tmp.seek(0)
        subprocess.Popen(
            _watchdog_argv(cmd),
            stdin=tmp,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        return True
    except OSError:
        return False
    finally:
        tmp.close()
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def emit_block_output(reason: str) -> int:
    """ツール実行ブロックをハーネス別プロトコルで stdout/stderr に書き出す。

    ツール実行をブロックするフックは終了コードを直接返さず、このヘルパの
    戻り値を返すこと（Claude/Codex: stderr + exit 2、Copilot: deny JSON +
    exit 0 へ変換される）。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        フックが返すべき終了コード。

    Raises:
        例外は発生しません。
    """
    exit_code, deny_out, reason_err = emit_block(reason)
    if deny_out:
        write_stdout(deny_out)
    if reason_err:
        write_stderr(reason_err + "\n")
    return exit_code


def _emit_hook_specific_output(event_name: str, additional_context: str) -> str:
    """コンテキスト注入出力を実行中ハーネスのプロトコルで返す。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネスのプロトコルに適合した JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return adapt_context_output(event_name, additional_context)


def emit_session_start_output(additional_context: str = "") -> str:
    """SessionStart 用のフック出力 JSON 文字列を返す。

    ハーネスに応じたフォーマットを output_adapter 経由で選択する。
    stdout への書き込みは行わない純粋関数として使う。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネス別フォーマットの JSON 文字列。
        Claude Code: hookSpecificOutput ラッパー形式。
        Copilot CLI: {"additionalContext": "..."} トップレベル形式。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("SessionStart", additional_context)


def emit_user_prompt_submit_output(additional_context: str) -> str:
    """UserPromptSubmit 用のフック出力 JSON 文字列を返す。

    ハーネスに応じたフォーマットを output_adapter 経由で選択する。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネス別フォーマットの JSON 文字列。
        Claude Code: hookSpecificOutput ラッパー形式。
        Copilot CLI: {"additionalContext": "..."} トップレベル形式。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("UserPromptSubmit", additional_context)


def emit_post_tool_use_output(additional_context: str) -> str:
    """PostToolUse 用のフック出力 JSON 文字列を返す。

    ハーネスに応じたフォーマットを output_adapter 経由で選択する。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネス別フォーマットの JSON 文字列。
        Claude Code: hookSpecificOutput ラッパー形式。
        Copilot CLI: {"additionalContext": "..."} トップレベル形式。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("PostToolUse", additional_context)


def print_session_start_output(additional_context: str = "") -> None:
    """SessionStart 用のフック出力を stdout に書き出す。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        None

    Raises:
        例外は発生しません。
    """
    print(emit_session_start_output(additional_context))
