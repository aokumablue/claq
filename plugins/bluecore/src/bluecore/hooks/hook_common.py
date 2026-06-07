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
    """標準入力から生のテキストを読み取ります。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列を返します。

    Raises:
        例外は発生しません。
    """
    return sys.stdin.read(max_bytes)


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
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": additional_context,
            }
        },
        ensure_ascii=False,
    )


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
