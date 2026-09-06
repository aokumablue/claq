"""Markdown frontmatter を標準ライブラリだけで解析するモジュール。

先頭の ``---`` で囲まれた frontmatter を切り出す :func:`split_frontmatter` と、
YAML のサブセットを Python の値へ変換する :func:`parse_yaml` を提供する。

サポートする構文:

* ブロックマッピング（任意深さ・キーは常に ``str``）とブロックシーケンス
* コンパクトマップ（``- k: v``）とネストシーケンス
* フローコレクション ``[a, b]`` / ``{k: v}``（相互ネスト可・深さ上限 32）
* 単一引用符・二重引用符（``\\n`` ``\\t`` ``\\xHH`` ``\\uHHHH`` などのエスケープ）
* ブロックスカラー ``|`` / ``>`` とチョンピング ``-`` / ``+``
* プレーンスカラーの型解決（null / bool / int / float / str）と ``␣#`` コメント除去

サポートしない構文（いずれも :class:`FrontmatterError` を送出し、黙って誤解釈しない）:
アンカー・エイリアス・タグ・複合キー（``? ``）・ディレクティブ・複数ドキュメント・
フロー内の暗黙マップ・複数行にまたがるフロー・希少エスケープ・
ブロックスカラーの明示インデント指示子・インデントへのタブ。

pyyaml との意図的な差分: ``yes`` / ``no`` / ``on`` / ``off`` は bool にせず str のまま扱い
（最終消費者である Claude Code の YAML 1.2 core schema に合わせる）、``12:30`` や
``2026-08-09`` も str のままとする。マッピングのキーは型解決せず常に str へ落とす。

入力テキストは ``\\n`` 区切りの行として解釈する（末尾の改行は空行 1 行として数える）。
"""

from __future__ import annotations

import re

__all__ = [
    "FrontmatterError",
    "MissingFrontmatterError",
    "UnterminatedFrontmatterError",
    "parse_yaml",
    "split_frontmatter",
]

_FENCE = "---"
_MAX_DEPTH = 32
_FLOW_END = ",]}"
_UNSUPPORTED_PREFIXES = ("&", "*", "!")
_NULL_LITERALS = frozenset({"", "~", "null", "Null", "NULL"})
_TRUE_LITERALS = frozenset({"true", "True", "TRUE"})
_FALSE_LITERALS = frozenset({"false", "False", "FALSE"})
_INT_RE = re.compile(r"^[-+]?[0-9]+$")
_FLOAT_RE = re.compile(r"^[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?$|^[-+]?[0-9]+[eE][-+]?[0-9]+$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]+$")
_SIMPLE_ESCAPES = {
    "\\": "\\",
    '"': '"',
    "/": "/",
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "b": "\b",
    "f": "\f",
    "0": "\0",
}


class FrontmatterError(ValueError):
    """frontmatter の切り出しまたは YAML 解析に失敗したことを表す例外。"""


class MissingFrontmatterError(FrontmatterError):
    """先頭の ``---`` が無く frontmatter が存在しないことを表す例外。"""


class UnterminatedFrontmatterError(FrontmatterError):
    """終了側の ``---`` が無く frontmatter が閉じていないことを表す例外。"""


def split_frontmatter(content: str) -> str:
    """Markdown 本文から frontmatter の生テキストを切り出して返す。

    BOM を除去し CRLF / CR を LF へ正規化したうえで、先頭の ``---`` 行と次に現れる
    ``---`` 行の間を返す。フェンスは **列 0 から始まり** ``---`` だけからなる行に限る。
    ``---extra`` のような行はフェンスとみなさない。

    行頭にインデントのある ``---`` をフェンスとみなさない理由（H-13）:
        以前は ``strip()`` 一致で探していたため列 0 を要求せず、frontmatter の
        内側に現れたインデント付き ``---`` を終了フェンスとして誤認していた。
        実測: ``---\\ndescription: |\\n  ---\\nname: x\\ntools: Read\\n---`` は
        ブロックスカラーの中身である ``  ---`` で切られ、``description`` が空文字列に
        なったうえ ``name`` と ``tools`` が本文へ落ちていた。ブロックスカラー・
        ブロックシーケンス・ネストしたマッピングの値はいずれもインデントされる
        ため、インデント付き ``---`` は原理的に frontmatter の**中身**であって
        区切りではない。

    行末の空白は従来どおり許容する。フェンス行が列 0 から始まっている限り末尾の
    空白はエディタ上で不可視であり、これを拒否しても防げる誤認は無い（誤認の
    原因は行頭側にしか無い）。

    Args:
        content: Markdown ファイル全体のテキスト。

    Returns:
        frontmatter の生テキスト。空の frontmatter では空文字列。

    Raises:
        MissingFrontmatterError: 先頭行が ``---`` でない場合。
        UnterminatedFrontmatterError: 終了側の ``---`` が見つからない場合。
    """
    normalized = content.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines[0].rstrip() != _FENCE:
        raise MissingFrontmatterError("frontmatter の開始 --- がありません")
    for index, line in enumerate(lines[1:], start=1):
        if line.rstrip() == _FENCE:
            return "\n".join(lines[1:index])
    raise UnterminatedFrontmatterError("frontmatter の終了 --- がありません")


def parse_yaml(text: str, *, lenient: bool = False) -> object:
    """YAML のサブセットを解析して Python の値へ変換する。

    Args:
        text: 解析対象のテキスト（``\\n`` 区切りの行として解釈する）。
        lenient: True のとき解釈できないエントリを読み飛ばして部分結果を返す。
            このモードでは :class:`FrontmatterError` を一切送出しない。

    Returns:
        ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` のいずれか。
        内容が空なら ``None``。

    Raises:
        FrontmatterError: ``lenient`` が False で構文を解釈できない場合。
    """
    return _Parser(text.split("\n"), lenient=lenient).parse()


def _is_blank(line: str) -> bool:
    """空行または行全体コメントであるかを返す。"""
    stripped = line.strip()
    return stripped == "" or stripped.startswith("#")


def _indent_width(line: str) -> int:
    """行頭の空白とタブの合計文字数を返す（タブ検査はしない）。"""
    return len(line) - len(line.lstrip(" \t"))


def _check_depth(depth: int) -> None:
    """ネスト深さが上限を超えていないか検査する。

    Raises:
        FrontmatterError: 深さが上限を超えた場合。
    """
    if depth > _MAX_DEPTH:
        raise FrontmatterError(f"ネストが深すぎます（上限 {_MAX_DEPTH}）")


def _check_unsupported(text: str) -> None:
    """アンカー / エイリアス / タグの開始記号を検出して拒否する。

    Raises:
        FrontmatterError: ``&`` ``*`` ``!`` で始まる場合。
    """
    if text[:1] in _UNSUPPORTED_PREFIXES:
        raise FrontmatterError(f"'{text[:1]}' で始まる構文（アンカー/エイリアス/タグ）はサポートしません")


def _strip_comment(text: str) -> str:
    """プレーンスカラーから空白直前の ``#`` 以降を取り除いて右 strip する。"""
    for index, char in enumerate(text):
        if char == "#" and index > 0 and text[index - 1] in " \t":
            return text[:index].rstrip()
    return text.rstrip()


def _resolve_plain(text: str) -> object:
    """プレーンスカラーの文字列を Python の値へ型解決する。

    bool はリテラル完全一致のみで判定し、int より先に判定する（``1`` が bool に
    ならないことを構造的に保証する）。

    Raises:
        FrontmatterError: 未サポート構文、またはコロンを含む場合。
    """
    _check_unsupported(text)
    if ": " in text or text.endswith(":"):
        raise FrontmatterError("プレーンスカラーに ':' を含めることはできません")
    if text in _NULL_LITERALS:
        return None
    if text in _TRUE_LITERALS:
        return True
    if text in _FALSE_LITERALS:
        return False
    if _INT_RE.match(text):
        return int(text)
    if _FLOAT_RE.match(text):
        return float(text)
    return text


def _read_escape(text: str, index: int) -> tuple[str, int]:
    """二重引用符内のバックスラッシュエスケープを 1 つ解釈する。

    Args:
        text: 対象テキスト。
        index: バックスラッシュの次の位置。

    Returns:
        解釈した文字と、その次の位置。

    Raises:
        FrontmatterError: エスケープが不完全、未サポート、または不正な 16 進表記の場合。
    """
    if index >= len(text):
        raise FrontmatterError("エスケープが途中で終わっています")
    char = text[index]
    if char in _SIMPLE_ESCAPES:
        return _SIMPLE_ESCAPES[char], index + 1
    if char in ("x", "u"):
        width = 2 if char == "x" else 4
        digits = text[index + 1 : index + 1 + width]
        if len(digits) != width or not _HEX_RE.match(digits):
            raise FrontmatterError(f"\\{char} エスケープの 16 進表記が不正です")
        return chr(int(digits, 16)), index + 1 + width
    raise FrontmatterError(f"サポートしないエスケープ \\{char} です")


def _read_single_quoted(text: str, start: int) -> tuple[str, int]:
    """単一引用符文字列を読み取り (値, 閉じ引用符の次の位置) を返す。

    Raises:
        FrontmatterError: 引用符が閉じていない場合。
    """
    parts: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == "'":
            if index + 1 < len(text) and text[index + 1] == "'":
                parts.append("'")
                index += 2
                continue
            return "".join(parts), index + 1
        parts.append(char)
        index += 1
    raise FrontmatterError("単一引用符が閉じていません")


def _read_double_quoted(text: str, start: int) -> tuple[str, int]:
    """二重引用符文字列を読み取り (値, 閉じ引用符の次の位置) を返す。

    Raises:
        FrontmatterError: 引用符が閉じていない場合。
    """
    parts: list[str] = []
    index = start + 1
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(parts), index + 1
        if char == "\\":
            value, index = _read_escape(text, index + 1)
            parts.append(value)
            continue
        parts.append(char)
        index += 1
    raise FrontmatterError("二重引用符が閉じていません")


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """引用符付き文字列を読み取り (値, 閉じ引用符の次の位置) を返す。"""
    if text[start] == "'":
        return _read_single_quoted(text, start)
    return _read_double_quoted(text, start)


def _ensure_no_trailing(text: str, index: int) -> None:
    """引用符・フローコレクションの直後に余分なトークンが無いことを確認する。

    Raises:
        FrontmatterError: コメント以外の余剰トークンがある場合。
    """
    rest = text[index:].strip()
    if rest and not rest.startswith("#"):
        raise FrontmatterError("値の後ろに余分なトークンがあります")


def _find_key_separator(text: str) -> int | None:
    """マッピングのキーと値を区切る ``:`` の位置を返す。無ければ None。

    引用符の内側とフローコレクションの内側は走査対象から外し、空白直前の ``#``
    でコメントとして打ち切る。

    Raises:
        FrontmatterError: 引用符が閉じていない場合。
    """
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in "'\"":
            _, index = _read_quoted(text, index)
            continue
        if char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "#" and index > 0 and text[index - 1] in " \t":
            break
        elif char == ":" and depth == 0 and (index + 1 == len(text) or text[index + 1] in " \t"):
            return index
        index += 1
    return None


def _parse_key(text: str) -> str:
    """マッピングのキーを常に ``str`` として解釈する。"""
    stripped = text.strip()
    if stripped[:1] in ("'", '"'):
        key, end = _read_quoted(stripped, 0)
        _ensure_no_trailing(stripped, end)
        return key
    _check_unsupported(stripped)
    return _strip_comment(stripped)


def _parse_block_header(text: str) -> tuple[str, str]:
    """ブロックスカラーのヘッダから (スタイル, チョンピング指示) を返す。

    Raises:
        FrontmatterError: 明示インデント指示子など未サポートの指示がある場合。
    """
    indicator = _strip_comment(text[1:]).strip()
    if indicator not in ("", "-", "+"):
        raise FrontmatterError("ブロックスカラーの明示インデント指示子はサポートしません")
    return text[0], indicator


def _fold_lines(kept: list[str]) -> str:
    """``>`` の折り畳み規則で行リストを 1 つの文字列へ連結する。

    通常行どうしの改行は空白 1 個へ畳み、空行 k 個を挟む場合は改行 k 個にする。
    どちらかがより深いインデントの行なら畳まず、改行をそのまま残す。
    """
    if not kept:
        return ""
    parts = [kept[0]]
    index = 1
    while index < len(kept):
        blanks = 0
        while index < len(kept) and kept[index] == "":
            blanks += 1
            index += 1
        line = kept[index]
        previous = kept[index - blanks - 1]
        folded = not previous.startswith(" ") and not line.startswith(" ")
        count = blanks if folded else blanks + 1
        parts.append(" " if count == 0 else "\n" * count)
        parts.append(line)
        index += 1
    return "".join(parts) + "\n"


def _skip_spaces(text: str, index: int) -> int:
    """空白とタブを読み飛ばした位置を返す。"""
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _skip_flow_comma(text: str, index: int) -> int:
    """フローコレクションの要素区切りカンマを読み飛ばす。

    Raises:
        FrontmatterError: カンマが無い、またはカンマの後で入力が尽きた場合。
    """
    if text[index] != ",":
        raise FrontmatterError("フローコレクションの区切りが不正です")
    index = _skip_spaces(text, index + 1)
    if index >= len(text):
        raise FrontmatterError("フローコレクションが閉じていません")
    return index


def _read_flow_key(text: str, index: int) -> tuple[str, int]:
    """フローマッピングのキーを読み取り (キー, 次の位置) を返す。

    Raises:
        FrontmatterError: キーが空、または未サポート構文で始まる場合。
    """
    if text[index] in "'\"":
        return _read_quoted(text, index)
    end = index
    while end < len(text) and text[end] not in ":,]}":
        end += 1
    key = text[index:end].strip()
    if not key:
        raise FrontmatterError("フローマッピングのキーが空です")
    _check_unsupported(key)
    return key, end


class _Parser:
    """行リストを先頭から消費して YAML サブセットを解析する再帰下降パーサ。"""

    def __init__(self, lines: list[str], *, lenient: bool) -> None:
        """解析対象の行リストと寛容モードの有無を保持する。"""
        self.lines = lines
        self.lenient = lenient
        self.pos = 0

    def parse(self) -> object:
        """文書全体を解析して最上位の値を返す。"""
        self._skip_blank_lines()
        if self.pos >= len(self.lines):
            return None
        try:
            value = self._parse_node(self._indent_of(self.lines[self.pos]), 1)
        except FrontmatterError:
            if not self.lenient:
                raise
            return None
        self._skip_blank_lines()
        if self.pos < len(self.lines) and not self.lenient:
            raise FrontmatterError("最上位ブロックより浅いインデントの行があります")
        return value

    def _skip_blank_lines(self) -> None:
        """空行と行全体コメントを読み飛ばす。"""
        while self.pos < len(self.lines) and _is_blank(self.lines[self.pos]):
            self.pos += 1

    def _indent_of(self, line: str) -> int:
        """行のインデント幅を返す。

        Raises:
            FrontmatterError: インデントにタブが含まれる場合。
        """
        width = _indent_width(line)
        if "\t" in line[:width]:
            raise FrontmatterError("インデントにタブは使えません")
        return width

    def _recover(self, start: int, indent: int) -> None:
        """解析に失敗したエントリを読み飛ばし、次の同階層行まで進める。

        必ず開始行の次から再開し、続くより深いインデントの行を捨てる。
        """
        self.pos = start + 1
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip() != "" and _indent_width(line) <= indent:
                break
            self.pos += 1

    def _parse_node(self, indent: int, depth: int) -> object:
        """指定インデントで始まるブロックノードを解析する。"""
        _check_depth(depth)
        rest = self.lines[self.pos][indent:]
        if rest.rstrip() == "-" or rest.startswith("- "):
            return self._parse_sequence(indent, depth)
        if rest.startswith("? ") or _find_key_separator(rest) is not None:
            return self._parse_mapping(indent, depth)
        self.pos += 1
        return self._parse_value(rest, indent - 1, depth)

    def _parse_mapping(self, indent: int, depth: int) -> dict[str, object]:
        """同一インデントに並ぶ ``key: value`` を辞書として解析する。

        Raises:
            FrontmatterError: 厳格モードでエントリを解釈できない、またはキーが重複した場合。
        """
        result: dict[str, object] = {}
        while True:
            self._skip_blank_lines()
            if self.pos >= len(self.lines):
                break
            start = self.pos
            if _indent_width(self.lines[start]) < indent:
                break
            try:
                key, value = self._parse_mapping_entry(indent, depth)
            except FrontmatterError:
                if not self.lenient:
                    raise
                self._recover(start, indent)
                continue
            if key in result:
                if not self.lenient:
                    raise FrontmatterError(f"キー '{key}' が重複しています")
            else:
                result[key] = value
        return result

    def _parse_mapping_entry(self, indent: int, depth: int) -> tuple[str, object]:
        """マッピングの 1 エントリを解析して (キー, 値) を返す。

        Raises:
            FrontmatterError: インデント不一致、複合キー、コロン欠落の場合。
        """
        line = self.lines[self.pos]
        if self._indent_of(line) != indent:
            raise FrontmatterError("インデントがマッピングの階層と一致しません")
        rest = line[indent:]
        if rest.startswith("? "):
            raise FrontmatterError("複合キー（? 記法）はサポートしません")
        separator = _find_key_separator(rest)
        if separator is None:
            raise FrontmatterError("マッピングの行に ':' がありません")
        key = _parse_key(rest[:separator])
        self.pos += 1
        return key, self._parse_value(rest[separator + 1 :], indent, depth)

    def _parse_sequence(self, indent: int, depth: int) -> list[object]:
        """同一インデントに並ぶ ``- `` エントリをリストとして解析する。

        Raises:
            FrontmatterError: 厳格モードでエントリを解釈できない場合。
        """
        items: list[object] = []
        while True:
            self._skip_blank_lines()
            if self.pos >= len(self.lines):
                break
            start = self.pos
            if _indent_width(self.lines[start]) < indent:
                break
            try:
                items.append(self._parse_sequence_entry(indent, depth))
            except FrontmatterError:
                if not self.lenient:
                    raise
                self._recover(start, indent)
        return items

    def _parse_sequence_entry(self, indent: int, depth: int) -> object:
        """シーケンスの 1 エントリを解析して値を返す。

        ダッシュとその直後の空白を行リスト上で空白へ潰し、要素の内容が始まる桁を
        インデントとしてブロックノードへ再帰する。これでコンパクトマップと
        ネストシーケンスが追加のコードなしに解釈できる。

        Raises:
            FrontmatterError: インデント不一致、または ``- `` で始まらない場合。
        """
        line = self.lines[self.pos]
        if self._indent_of(line) != indent:
            raise FrontmatterError("インデントがシーケンスの階層と一致しません")
        rest = line[indent:]
        if rest.rstrip() == "-":
            self.pos += 1
            return self._parse_block_value(indent, depth)
        if not rest.startswith("- "):
            raise FrontmatterError("シーケンス要素は '- ' で始まる必要があります")
        content = indent + len(rest) - len(rest[1:].lstrip(" "))
        self.lines[self.pos] = " " * content + line[content:]
        return self._parse_node(content, depth + 1)

    def _parse_block_value(self, base_indent: int, depth: int) -> object:
        """次行以降のより深いインデントをブロックノードとして解析する。

        より深い行が無ければ ``None`` を返す。
        """
        self._skip_blank_lines()
        if self.pos >= len(self.lines):
            return None
        child = self._indent_of(self.lines[self.pos])
        if child <= base_indent:
            return None
        return self._parse_node(child, depth + 1)

    def _parse_value(self, text: str, base_indent: int, depth: int) -> object:
        """``key:`` の右側テキストから値を解析する。

        呼び出し時点で ``self.pos`` は次の行を指している前提。

        Args:
            text: 値として解釈するテキスト。
            base_indent: 継続行がこれより深いインデントであることを要求する基準桁。
            depth: 現在のネスト深さ。

        Returns:
            解析した値。

        Raises:
            FrontmatterError: 値を解釈できない場合。
        """
        rest = text.strip()
        if rest == "" or rest.startswith("#"):
            return self._parse_block_value(base_indent, depth)
        if rest[0] in "|>":
            style, chomping = _parse_block_header(rest)
            return self._read_block_scalar(style, chomping, base_indent)
        if rest[0] in "'\"":
            value, end = _read_quoted(rest, 0)
            _ensure_no_trailing(rest, end)
            return value
        if rest[0] in "[{":
            value, end = self._read_flow(rest, 0, depth + 1)
            _ensure_no_trailing(rest, end)
            return value
        return self._read_plain_scalar(rest, base_indent)

    def _read_plain_scalar(self, first: str, base_indent: int) -> object:
        """プレーンスカラーを継続行とともに読み取り、型解決した値を返す。

        継続行は空白 1 個で連結する。空行・行全体コメント・浅いインデントで終端する。
        """
        parts = [_strip_comment(first)]
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if _is_blank(line):
                break
            if self._indent_of(line) <= base_indent:
                break
            parts.append(_strip_comment(line.strip()))
            self.pos += 1
        return _resolve_plain(" ".join(parts))

    def _read_block_scalar(self, style: str, chomping: str, base_indent: int) -> str:
        """``|`` / ``>`` のブロックスカラー本文を読み取って文字列を返す。

        コンテンツのインデントはヘッダ直後の最初の非空行のインデントを採用する。

        Args:
            style: ``|``（リテラル）または ``>``（折り畳み）。
            chomping: ``-``（strip）/ ``+``（keep）/ ``""``（clip）。
            base_indent: 本文がこれより深いインデントであることを要求する基準桁。

        Returns:
            チョンピング適用後の文字列。
        """
        content: list[str] = []
        content_indent: int | None = None
        while self.pos < len(self.lines):
            line = self.lines[self.pos]
            if line.strip() == "":
                content.append("")
                self.pos += 1
                continue
            indent = self._indent_of(line)
            if indent <= base_indent:
                break
            if content_indent is None:
                content_indent = indent
            if indent < content_indent:
                break
            content.append(line[content_indent:])
            self.pos += 1
        end = len(content)
        while end > 0 and content[end - 1] == "":
            end -= 1
        kept = content[:end]
        trailing = len(content) - end
        if style == "|":
            body = "".join(line + "\n" for line in kept)
        else:
            body = _fold_lines(kept)
        if chomping == "-":
            return body.rstrip("\n")
        if chomping == "+":
            return body + "\n" * trailing
        return body

    def _read_flow(self, text: str, start: int, depth: int) -> tuple[object, int]:
        """フローコレクションを解析して (値, 閉じ括弧の次の位置) を返す。

        Raises:
            FrontmatterError: 深さが上限を超えた、または構文が不正な場合。
        """
        _check_depth(depth)
        if text[start] == "[":
            return self._read_flow_sequence(text, start, depth)
        return self._read_flow_mapping(text, start, depth)

    def _read_flow_sequence(self, text: str, start: int, depth: int) -> tuple[list[object], int]:
        """``[a, b]`` 形式のフローシーケンスを解析する。

        末尾カンマは許容する（pyyaml 互換）。

        Raises:
            FrontmatterError: 閉じ括弧が無い、または区切りが不正な場合。
        """
        items: list[object] = []
        index = start + 1
        first = True
        while True:
            index = _skip_spaces(text, index)
            if index >= len(text):
                raise FrontmatterError("フローシーケンスが閉じていません")
            if text[index] == "]":
                return items, index + 1
            if not first:
                index = _skip_flow_comma(text, index)
                if text[index] == "]":
                    return items, index + 1
            value, index = self._read_flow_item(text, index, depth)
            items.append(value)
            first = False

    def _read_flow_mapping(self, text: str, start: int, depth: int) -> tuple[dict[str, object], int]:
        """``{k: v}`` 形式のフローマッピングを解析する。

        末尾カンマは許容する（pyyaml 互換）。キーの重複は厳格モードでエラー、
        寛容モードでは先勝ちで黙認する。

        Raises:
            FrontmatterError: 閉じ括弧が無い、区切りが不正、またはキーが重複した場合。
        """
        result: dict[str, object] = {}
        index = start + 1
        first = True
        while True:
            index = _skip_spaces(text, index)
            if index >= len(text):
                raise FrontmatterError("フローマッピングが閉じていません")
            if text[index] == "}":
                return result, index + 1
            if not first:
                index = _skip_flow_comma(text, index)
                if text[index] == "}":
                    return result, index + 1
            key, index = _read_flow_key(text, index)
            index = _skip_spaces(text, index)
            if index >= len(text) or text[index] != ":":
                raise FrontmatterError("フローマッピングのキーに ':' が続いていません")
            value, index = self._read_flow_item(text, _skip_spaces(text, index + 1), depth)
            if key in result:
                if not self.lenient:
                    raise FrontmatterError(f"キー '{key}' が重複しています")
            else:
                result[key] = value
            first = False

    def _read_flow_item(self, text: str, index: int, depth: int) -> tuple[object, int]:
        """フローコレクションの要素を 1 つ読み取り (値, 次の位置) を返す。

        Raises:
            FrontmatterError: 要素が空、暗黙マップ、または区切りが不正な場合。
        """
        if index >= len(text):
            raise FrontmatterError("フローコレクションが閉じていません")
        char = text[index]
        if char in "'\"":
            value, end = _read_quoted(text, index)
        elif char in "[{":
            value, end = self._read_flow(text, index, depth + 1)
        else:
            end = index
            while end < len(text) and text[end] not in _FLOW_END:
                end += 1
            raw = text[index:end].strip()
            if not raw:
                raise FrontmatterError("フローコレクションに空の要素があります")
            if ": " in raw or raw.endswith(":"):
                raise FrontmatterError("フロー内の暗黙マップはサポートしません")
            return _resolve_plain(raw), end
        end = _skip_spaces(text, end)
        if end >= len(text) or text[end] not in _FLOW_END:
            raise FrontmatterError("フローコレクションの区切りが不正です")
        return value, end
