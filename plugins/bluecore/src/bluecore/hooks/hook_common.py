"""bluecoreフック実装の共通ユーティリティ。

フック用の入力読み込み、JSON解析、
出力書き込みの共有関数を提供します。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

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


def _emit_hook_specific_output(event_name: str, additional_context: str) -> str:
    """hookSpecificOutput でラップした JSON 文字列を返す。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        hookSpecificOutput を含む JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            }
        },
        ensure_ascii=False,
    )


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
