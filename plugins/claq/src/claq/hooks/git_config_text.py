"""git config の本文を最小限に解析し、フック実行を左右する key の変更を判定する。

`config_protection` が ``.git/config`` や ``~/.gitconfig`` への Edit/Write の「置換前の全文」と
「置換後の全文」を比べるために使う葉モジュール。key の危険判定は呼び出し側が関数で渡すため、
他のフックモジュールを import しない。

解析は git 本体の字句規則（``config.c`` の ``git_parse_source``）に合わせる。本体と読み方が
食い違うと、本体が key として読む行を見落として素通りさせるため、解釈の曖昧な本文は
`GitConfigParseError` にして呼び出し側へ伝える（deny に倒す）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import NamedTuple

# git が空白として扱う文字（``isspace``）。改行は行の区切りとして別に扱う。
_BLANKS = " \t\r\v\f"
_COMMENT_STARTS = "#;"
# 値の中のバックスラッシュエスケープ（``\\`` ``\"`` ``\n`` ``\t`` ``\b``）。``\<改行>`` は継続行。
_VALUE_ESCAPES = {"n": "\n", "t": "\t", "b": "\b", "\\": "\\", '"': '"'}


class GitConfigParseError(ValueError):
    """git config として解釈できない本文であることを表す。"""


class ConfigEntry(NamedTuple):
    """解析した 1 つの key と値。

    Attributes:
        key: ``section[.subsection].name`` の完全修飾名。section と name は小文字、``[sec "Sub"]``
            形式の subsection は大小を保つ（git の規則どおり）。
        value: 値。``=`` の無い key は None。
    """

    key: str
    value: str | None


def _is_key_char(char: str) -> bool:
    """key 名・section 名に使える文字（ASCII 英数字と ``-``）か判定する。

    Args:
        char: 1 文字。

    Returns:
        使える文字なら True。

    Raises:
        例外は発生しません。
    """
    return char.isascii() and (char.isalnum() or char == "-")


def _skip_line(text: str, pos: int) -> int:
    """次の行頭（改行の直後）の位置を返す。改行が無ければ末尾。

    Args:
        text: 解析対象の本文。
        pos: 検索開始位置。

    Returns:
        次の行頭の位置。

    Raises:
        例外は発生しません。
    """
    newline = text.find("\n", pos)
    return len(text) if newline < 0 else newline + 1


def _parse_header(text: str, pos: int) -> tuple[str, int]:
    """``[`` の直後から見出しを読み、key の前置（``section.`` / ``section.sub.``）を返す。

    ``[sec]`` と ``[sec "Sub"]``、旧記法の ``[sec.sub]``（全体を小文字にする）を受け付ける。

    Args:
        text: 解析対象の本文。
        pos: ``[`` の直後の位置。

    Returns:
        key の前置（末尾の ``.`` を含む）と、``]`` の直後の位置。

    Raises:
        GitConfigParseError: 見出しが閉じない・使えない文字を含む・名前が空。
    """
    length = len(text)
    name: list[str] = []
    while True:
        if pos >= length:
            raise GitConfigParseError("unterminated section header")
        char = text[pos]
        pos += 1
        if char == "]":
            break
        if char.isspace():
            return _parse_extended_header("".join(name), text, pos - 1)
        if not (_is_key_char(char) or char == "."):
            raise GitConfigParseError(f"invalid character in section header: {char!r}")
        name.append(char.lower())
    if not name:
        raise GitConfigParseError("empty section name")
    return "".join(name) + ".", pos


def _parse_extended_header(base: str, text: str, pos: int) -> tuple[str, int]:
    """``[sec "Sub"]`` 形式の見出しの、名前の後ろの部分を読む。

    Args:
        base: 小文字化済みの section 名（空でありうる）。
        text: 解析対象の本文。
        pos: 名前の直後にある空白の位置。

    Returns:
        key の前置（``section.Sub.``）と、``]`` の直後の位置。

    Raises:
        GitConfigParseError: 引用符が無い・閉じない・途中で改行する・``]`` が続かない・名前が空。
    """
    length = len(text)
    while pos < length and text[pos].isspace():
        if text[pos] == "\n":
            raise GitConfigParseError("newline in section header")
        pos += 1
    if pos >= length or text[pos] != '"':
        raise GitConfigParseError("subsection must be quoted")
    pos += 1
    subsection: list[str] = []
    while True:
        if pos >= length or text[pos] == "\n":
            raise GitConfigParseError("unterminated subsection")
        char = text[pos]
        pos += 1
        if char == '"':
            break
        if char == "\\":
            if pos >= length or text[pos] == "\n":
                raise GitConfigParseError("unterminated subsection")
            char = text[pos]
            pos += 1
        subsection.append(char)
    if pos >= length or text[pos] != "]":
        raise GitConfigParseError("section header is not closed")
    if not base:
        raise GitConfigParseError("empty section name")
    return f"{base}.{''.join(subsection)}.", pos + 1


def _parse_value(text: str, pos: int) -> tuple[str, int]:
    """``=`` の直後から値を読む（引用符・エスケープ・継続行・行内コメントを解釈する）。

    Args:
        text: 解析対象の本文。
        pos: ``=`` の直後の位置。

    Returns:
        値と、値を終えた行の次の行頭の位置（末尾なら本文の長さ）。

    Raises:
        GitConfigParseError: 引用符が閉じないまま行が終わる・未知のエスケープ・末尾の ``\\``。
    """
    length = len(text)
    value: list[str] = []
    pending_spaces = 0
    quoted = False
    in_comment = False
    while pos < length:
        char = text[pos]
        pos += 1
        if char == "\n":
            if quoted:
                raise GitConfigParseError("unterminated quote in value")
            return "".join(value), pos
        if in_comment:
            continue
        if char.isspace() and not quoted:
            if value:
                pending_spaces += 1
            continue
        if not quoted and char in _COMMENT_STARTS:
            in_comment = True
            continue
        value.append(" " * pending_spaces)
        pending_spaces = 0
        if char == "\\":
            if pos >= length:
                raise GitConfigParseError("trailing backslash in value")
            escaped = text[pos]
            pos += 1
            if escaped == "\n":
                continue
            if escaped not in _VALUE_ESCAPES:
                raise GitConfigParseError(f"unknown escape in value: {escaped!r}")
            value.append(_VALUE_ESCAPES[escaped])
        elif char == '"':
            quoted = not quoted
        else:
            value.append(char)
    if quoted:
        raise GitConfigParseError("unterminated quote in value")
    return "".join(value), pos


def parse_git_config(text: str) -> list[ConfigEntry]:
    """git config の本文を (完全修飾 key, 値) の列に解析する。

    コメント（``#`` / ``;``）・空行・CRLF・先頭の BOM を読み飛ばし、``[core] hooksPath = x`` の
    ように見出しと同じ行にある key も拾う。見出しより前の key・key 名として使えない行は
    git 本体も受け付けないため解析エラーにする。

    Args:
        text: 解析対象の本文。

    Returns:
        本文に現れた順の `ConfigEntry`。

    Raises:
        GitConfigParseError: git config として解釈できない行がある。
    """
    text = text.removeprefix("﻿").replace("\r\n", "\n")
    length = len(text)
    entries: list[ConfigEntry] = []
    prefix: str | None = None
    pos = 0
    while pos < length:
        char = text[pos]
        if char.isspace():
            pos += 1
        elif char in _COMMENT_STARTS:
            pos = _skip_line(text, pos)
        elif char == "[":
            prefix, pos = _parse_header(text, pos + 1)
        elif char.isascii() and char.isalpha():
            if prefix is None:
                raise GitConfigParseError("key before any section header")
            start = pos
            while pos < length and _is_key_char(text[pos]):
                pos += 1
            key = prefix + text[start:pos].lower()
            while pos < length and text[pos] in _BLANKS:
                pos += 1
            if pos < length and text[pos] == "=":
                value, pos = _parse_value(text, pos + 1)
                entries.append(ConfigEntry(key, value))
            elif pos >= length or text[pos] == "\n":
                entries.append(ConfigEntry(key, None))
            else:
                raise GitConfigParseError(f"unexpected character after key: {text[pos]!r}")
        else:
            raise GitConfigParseError(f"unrecognized line starting with {char!r}")
    return entries


def _sensitive_entries(text: str, is_sensitive: Callable[[str], bool]) -> list[ConfigEntry]:
    """本文の中の危険 key のエントリを、現れた順に返す。

    Args:
        text: 解析対象の本文。
        is_sensitive: 完全修飾 key を受け取り、危険かを返す関数。

    Returns:
        危険なエントリの列。

    Raises:
        GitConfigParseError: 本文を解析できない。
    """
    return [entry for entry in parse_git_config(text) if is_sensitive(entry.key)]


def adds_sensitive_entries(before: str, after: str, is_sensitive: Callable[[str], bool]) -> bool:
    """置換後の本文が、置換前に無かった危険 key（または変わった実効値）を持つか判定する。

    置換後の危険エントリが置換前の部分列で、かつ key ごとの最後の値（実効値）が変わらない
    ときだけ False を返す。集合の包含では、後ろの上書きだけを消して実効値を危険側へ戻す編集や
    順序の入れ替えを通してしまうため、順序つきの列で比べる。危険 key を丸ごと消すだけの編集は
    通す。置換前が解析できない場合は、危険エントリが無かったものとして扱う（置換後の危険
    エントリは全て新規になる）。

    Args:
        before: 置換前の全文。
        after: 置換後の全文。
        is_sensitive: 完全修飾 key を受け取り、危険かを返す関数。

    Returns:
        新しく現れた、または実効値の変わった危険 key があれば True。

    Raises:
        GitConfigParseError: 置換後の本文を解析できない。
    """
    new = _sensitive_entries(after, is_sensitive)
    try:
        old = _sensitive_entries(before, is_sensitive)
    except GitConfigParseError:
        old = []
    remaining = iter(old)
    if not all(entry in remaining for entry in new):
        return True
    effective_old = {entry.key: entry.value for entry in old}
    return any(effective_old[entry.key] != entry.value for entry in new)
