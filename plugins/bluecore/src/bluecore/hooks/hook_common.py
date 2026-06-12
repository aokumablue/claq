"""bluecoreフック実装の共通ユーティリティ。

フック用の入力読み込み、JSON解析、
出力書き込みの共有関数を提供します。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from bluecore.hooks.output_adapter import adapt_context_output, emit_block

MAX_STDIN_BYTES = 1024 * 1024


def read_raw_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """標準入力から生のテキストをバイト単位の上限つきで読み取ります。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列（max_bytes バイトで切り捨て済み）を返します。

    Raises:
        例外は発生しません。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is not None:
        raw_bytes = stdin_buffer.read(max_bytes)
    else:
        # io.StringIO など .buffer を持たない stdin を想定した分岐。
        # 文字数 read ではバイト上限を最大 4 倍超過しうるためバイト換算で切り詰める。
        raw_bytes = sys.stdin.read(max_bytes).encode("utf-8", errors="replace")
    return raw_bytes[:max_bytes].decode("utf-8", errors="replace")


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


# SessionStart フックが stdout に出力すべき hookSpecificOutput を持つ hook_id 集合。
# run_with_flags は子の stdout が空のとき、この集合に含まれる hook_id のみ
# フォールバック JSON を出力する。新規 SessionStart hook を追加する際はここに追加する。
SESSION_START_HOOK_IDS: frozenset[str] = frozenset(
    {
        "session:start",
        "session:mem:setup",
        "session:mem:context",
        "session:mem:record-project-profile",
    }
)


# hooks.json で "async": true 指定されている run_with_flags 経由の hook_id 集合。
# Claude Code はホスト側で非同期実行するが、Codex 等 async 未サポートのハーネスでは
# run_with_flags が子プロセスを detach してフックを即時終了させる。
# hooks.json の async エントリを増減する際はここも更新する。
BACKGROUND_HOOK_IDS: frozenset[str] = frozenset(
    {
        "pre:observe",
        "user:mem:sync-check",
        "post:quality-gate",
        "stop:session-end",
        "stop:evaluate-session",
        "session:end:marker",
        "session:mem:end",
        "session:mem:sync-check",
    }
)


# detach 起動した子の実行時間上限（秒）。start_new_session=True の子はハーネスの
# timeout で kill されないため、coreutils timeout で自決させる。hooks.json の
# 最長エントリ（600 秒）より先に終わるよう 590 とする。
# --kill-after は GNU coreutils 固有のため Linux 前提（BSD/macOS の timeout は不可）。
DETACH_TIMEOUT_SECONDS = 590
# SIGTERM を無視して詰まったプロセスを SIGKILL で確実に回収するまでの猶予（秒）。
_DETACH_KILL_AFTER_SECONDS = 30


def detach_process(cmd: list[str], raw_stdin: str, *, env: dict[str, str] | None = None) -> bool:
    """コマンドを detached（新セッション）で起動し stdin を一時ファイル経由で渡す。

    親プロセスの終了に影響されず子を走らせ続けるために使う。一時ファイルは
    world-writable な /tmp を避けて ~/.bluecore 配下に作成し、close→reopen の
    TOCTOU 窓を作らないよう同一 fd を seek(0) して子へ継承する。起動直後に
    unlink する（継承済み fd は有効なまま）。

    detach 後の子はハーネスの timeout の管轄外になるため、coreutils timeout で
    ラップして DETACH_TIMEOUT_SECONDS で SIGTERM、さらに猶予後 SIGKILL を送り、
    暴走プロセスの無期限残留を防ぐ。

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
        private_dir = Path.home() / ".bluecore"
        private_dir.mkdir(parents=True, exist_ok=True)
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
            [
                "timeout",
                f"--kill-after={_DETACH_KILL_AFTER_SECONDS}",
                str(DETACH_TIMEOUT_SECONDS),
                *cmd,
            ],
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

    launcher から直接起動されるブロック系フック（run_with_flags の exit code
    変換を経由しないもの）はこのヘルパを使うこと。

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
    """SessionStart 用の hookSpecificOutput JSON 文字列を返す。

    stdout への書き込みは行わない純粋関数として使う。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        hookSpecificOutput を含む JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("SessionStart", additional_context)


def emit_user_prompt_submit_output(additional_context: str) -> str:
    """UserPromptSubmit 用の hookSpecificOutput JSON 文字列を返す。

    トップレベルに hookEventName を置く形式は Claude Code に構造化処理されず
    生 JSON のままコンテキストに注入されるため、必ずこのラッパー形式で出力する。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        hookSpecificOutput を含む JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return _emit_hook_specific_output("UserPromptSubmit", additional_context)


def print_session_start_output(additional_context: str = "") -> None:
    """SessionStart 用の hookSpecificOutput を stdout に出力する。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        None

    Raises:
        例外は発生しません。
    """
    print(emit_session_start_output(additional_context))
