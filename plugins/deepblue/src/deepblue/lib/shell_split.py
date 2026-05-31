"""
シェルコマンド文字列を区切り単位に分解します。
リダイレクトや区切り演算子を考慮しつつ、複数コマンドを安全に扱える形へ整理します。
フックやスクリプトで使う軽量パーサーです。
"""

from __future__ import annotations


def _advance_in_quote(
    command: str, i: int, length: int, ch: str, quote: str, current: str
) -> tuple[str, str | None, int]:
    """引用符内の1文字を処理し、(current, quote, next_i) を返す。"""
    if ch == "\\" and i + 1 < length:
        return current + ch + command[i + 1], quote, i + 2
    if ch == quote:
        return current + ch, None, i + 1
    return current + ch, quote, i + 1


def _try_flush_segment(current: str, segments: list[str]) -> str:
    """current が空でなければ segments に追加し、空文字列を返す。"""
    if current.strip():
        segments.append(current.strip())
    return ""


def _handle_ampersand(
    command: str, i: int, length: int, current: str, segments: list[str]
) -> tuple[str, int]:
    """単独の & を処理し、(current, next_i) を返す。リダイレクトは除外する。"""
    next_ch = command[i + 1] if i + 1 < length else ""
    prev_ch = command[i - 1] if i > 0 else ""
    if next_ch == ">" or prev_ch == ">":
        return current + "&", i + 1
    current = _try_flush_segment(current, segments)
    return current, i + 1


def split_shell_segments(command: str) -> list[str]:
    """シェルコマンドを演算子（&&, ||, ;, &）で分割する。
    ただし引用符（単/二重）とエスケープ文字は尊重する。
    リダイレクト演算子（&>, >&, 2>&1）は区切りとして扱わない。

    Args:
        command: command の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    segments: list[str] = []
    current = ""
    quote: str | None = None
    i = 0
    length = len(command)

    while i < length:
        ch = command[i]

        if quote:
            current, quote, i = _advance_in_quote(command, i, length, ch, quote, current)
            continue

        # 引用符外のバックスラッシュエスケープ
        if ch == "\\" and i + 1 < length:
            current += ch + command[i + 1]
            i += 2
            continue

        # 開始引用符
        if ch in ('"', "'"):
            quote = ch
            current += ch
            i += 1
            continue

        next_ch = command[i + 1] if i + 1 < length else ""

        if ch == "&" and next_ch == "&":
            current = _try_flush_segment(current, segments)
            i += 2
            continue

        if ch == "|" and next_ch == "|":
            current = _try_flush_segment(current, segments)
            i += 2
            continue

        if ch == ";":
            current = _try_flush_segment(current, segments)
            i += 1
            continue

        if ch == "&" and next_ch != "&":
            current, i = _handle_ampersand(command, i, length, current, segments)
            continue

        current += ch
        i += 1

    _try_flush_segment(current, segments)
    return segments


__all__ = ["split_shell_segments"]
