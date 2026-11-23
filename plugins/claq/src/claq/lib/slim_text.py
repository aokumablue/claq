"""引き継ぎ本文向けの 1 行整形ユーティリティ。

mem/handoff.py がユーザー発話を引き継ぎの 1 行へ畳むときに使う。文字は消さず、空白と
マークダウンの行頭記号だけを整える（無害化の後に掛けても、前後の文字を接着してタグを作らない）。
"""

from __future__ import annotations

TRUNCATION_MARKER = "..."
"""``compact_line`` が上限で切ったときに付ける末尾。"""


def _strip_markdown_prefix(line: str) -> str:
    """見出しや箇条書きの先頭記号を落とす。コードフェンス行と表の行は空にする。"""
    stripped = line.lstrip()
    if not stripped or stripped.startswith(("```", "|")):
        return ""

    if stripped[0] == "#":
        stripped = stripped.lstrip("#").strip()
    elif stripped.startswith(("- ", "* ", "+ ", "> ")):
        stripped = stripped[2:].strip()
    elif len(stripped) >= 3 and stripped[0].isdigit() and stripped[1:3] in {". ", ") "}:
        stripped = stripped[3:].strip()

    return stripped


def compact_line(text: str, max_length: int) -> str:
    """空白を畳みマークダウンの行頭記号を落として 1 行にし、長すぎれば切って ``...`` を付ける。"""
    value = _strip_markdown_prefix(" ".join(text.split())).strip()
    if len(value) > max_length:
        return value[:max_length].rstrip() + TRUNCATION_MARKER

    return value
