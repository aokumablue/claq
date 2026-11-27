"""引き継ぎ本文向けの 1 行整形ユーティリティ。

mem/handoff.py がユーザー発話を引き継ぎの 1 行へ畳むときに使う。文字は消さず、空白と
マークダウンの行頭記号だけを整える（無害化の後に掛けても、前後の文字を接着してタグを作らない）。
"""

from __future__ import annotations

TRUNCATION_MARKER = "..."
"""``compact_line`` が上限で切ったときに付ける末尾。"""

COMPACT_LINE_MAX_CHARS = 200
"""``compact_line`` が 1 行（ユーザー依頼 1 件）に残す上限文字数。"""


def _strip_markdown_prefix(line: str) -> str:
    """見出しや箇条書きの先頭記号を落とす。コードフェンス行と表の行は空にする。

    ``line`` は ``compact_line`` が空白を畳んだ 1 行（前後に空白が無く、語の間は空白 1 つ）に限る。
    そのため記号と直後の空白 1 つを切るだけで、前後に空白は残らない。
    """
    if not line or line.startswith(("```", "|")):
        return ""

    if line[0] == "#":
        return line.lstrip("#").lstrip()
    if line.startswith(("- ", "* ", "+ ", "> ")):
        return line[2:]
    if line[0].isdigit() and line[1:3] in {". ", ") "}:
        return line[3:]
    return line


def compact_line(text: str) -> str:
    """空白を畳みマークダウンの行頭記号を落として 1 行にし、``COMPACT_LINE_MAX_CHARS`` を超えれば切って ``...`` を付ける。"""
    value = _strip_markdown_prefix(" ".join(text.split()))
    if len(value) > COMPACT_LINE_MAX_CHARS:
        return value[:COMPACT_LINE_MAX_CHARS].rstrip() + TRUNCATION_MARKER

    return value
