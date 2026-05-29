"""
シェルコマンド文字列を区切り単位に分解します。
リダイレクトや区切り演算子を考慮しつつ、複数コマンドを安全に扱える形へ整理します。
フックやスクリプトで使う軽量パーサーです。
"""

from __future__ import annotations


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

        # 引用符内: エスケープと閉じ引用符を処理する
        if quote:
            if ch == "\\" and i + 1 < length:
                current += ch + command[i + 1]
                i += 2
                continue
            if ch == quote:
                quote = None
            current += ch
            i += 1
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
        prev_ch = command[i - 1] if i > 0 else ""

        # && 演算子
        if ch == "&" and next_ch == "&":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 2
            continue

        # || 演算子
        if ch == "|" and next_ch == "|":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 2
            continue

        # ; 区切り
        if ch == ";":
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 1
            continue

        # 単独の & — ただしリダイレクトパターン（&>, >&, digit>&）は除外する
        if ch == "&" and next_ch != "&":
            if next_ch == ">" or prev_ch == ">":
                current += ch
                i += 1
                continue
            if current.strip():
                segments.append(current.strip())
            current = ""
            i += 1
            continue

        current += ch
        i += 1

    if current.strip():
        segments.append(current.strip())

    return segments


__all__ = ["split_shell_segments"]
