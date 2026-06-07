"""ログ出力用の文字列サニタイズ。"""

from __future__ import annotations

import re

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]+")
_WHITESPACE = re.compile(r"\s+")


def sanitize_log_value(value: str, max_len: int = 200) -> str:
    """制御文字と改行を除去し、ログに載せやすい単一行へ整形する。

    Args:
        value: 整形対象の文字列。
        max_len: 出力の最大長。

    Returns:
        サニタイズ済み文字列。
    """
    text = str(value or "")
    text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    text = _CONTROL_CHARS.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if len(text) > max_len:
        return text[:max_len]
    return text
