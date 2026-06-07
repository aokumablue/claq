"""
シェルコマンド文字列を区切り単位に分解します。
リダイレクトや区切り演算子を考慮しつつ、複数コマンドを安全に扱える形へ整理します。
フックやスクリプトで使う軽量パーサーです。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class _ShellCtx:
    """パース対象のコマンド文字列と長さを保持するコンテキスト。"""

    command: str
    length: int


def _advance_in_quote(
    ctx: _ShellCtx, i: int, ch: str, quote: str, current: str
) -> tuple[str, str | None, int]:
    """引用符内の1文字を処理し、(current, quote, next_i) を返す。"""
    if ch == "\\" and i + 1 < ctx.length:
        return current + ch + ctx.command[i + 1], quote, i + 2
    if ch == quote:
        return current + ch, None, i + 1
    return current + ch, quote, i + 1


def _try_flush_segment(current: str, segments: list[str]) -> str:
    """current が空でなければ segments に追加し、空文字列を返す。"""
    if current.strip():
        segments.append(current.strip())
    return ""


def _handle_ampersand(
    ctx: _ShellCtx, i: int, current: str, segments: list[str]
) -> tuple[str, int]:
    """単独の & を処理し、(current, next_i) を返す。リダイレクトは除外する。"""
    next_ch = ctx.command[i + 1] if i + 1 < ctx.length else ""
    prev_ch = ctx.command[i - 1] if i > 0 else ""
    if next_ch == ">" or prev_ch == ">":
        return current + "&", i + 1
    current = _try_flush_segment(current, segments)
    return current, i + 1


def _handle_unquoted_char(
    ctx: _ShellCtx, i: int, ch: str, current: str, segments: list[str]
) -> tuple[str, str | None, int]:
    """引用符外の1文字を処理し、(current, new_quote, next_i) を返す。"""
    if ch == "\\" and i + 1 < ctx.length:
        return current + ch + ctx.command[i + 1], None, i + 2
    if ch in ('"', "'"):
        return current + ch, ch, i + 1
    next_ch = ctx.command[i + 1] if i + 1 < ctx.length else ""
    if ch == "&" and next_ch == "&":
        return _try_flush_segment(current, segments), None, i + 2
    if ch == "|" and next_ch == "|":
        return _try_flush_segment(current, segments), None, i + 2
    if ch == ";":
        return _try_flush_segment(current, segments), None, i + 1
    if ch == "&":
        new_current, new_i = _handle_ampersand(ctx, i, current, segments)
        return new_current, None, new_i
    return current + ch, None, i + 1


def split_shell_segments(command: str) -> list[str]:
    """シェルコマンドを &&, ||, ;, & で分割する（引用符・エスケープ考慮）。

    Args:
        command: 分割対象のシェルコマンド文字列

    Returns:
        分割されたセグメントのリスト
    """
    segments: list[str] = []
    current = ""
    quote: str | None = None
    i = 0
    ctx = _ShellCtx(command=command, length=len(command))
    while i < ctx.length:
        ch = command[i]
        if quote:
            current, quote, i = _advance_in_quote(ctx, i, ch, quote, current)
        else:
            current, quote, i = _handle_unquoted_char(ctx, i, ch, current, segments)
    _try_flush_segment(current, segments)
    return segments


__all__ = ["split_shell_segments"]
