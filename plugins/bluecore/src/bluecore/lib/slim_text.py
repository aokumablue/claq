"""通知・セッションサマリー向けの軽量テキスト圧縮ユーティリティ。

desktop_notify.py（通知本文 100 文字）と session_end.py（サマリー 200 文字）で使用する。
AI 応答本体の圧縮には使われない（フック制約上、事後圧縮は不可能）。
"""

from __future__ import annotations

_LEADING_PHRASES = (
    "ご質問ありがとうございます。",
    "お力になれれば幸いです。",
    "Sure!",
    "Certainly!",
    "Of course!",
    "I'd be happy to",
    "I'll help you with that",
    "Let me",
)

_FILLER_PHRASES = (
    "えーと",
    "まあ",
    "ちなみに",
    "一応",
    "とりあえず",
    "基本的に",
    "ざっくり言うと",
)

_COMPACTION_REPLACEMENTS = (
    ("することができる", "できる"),
    ("することができます", "できます"),
    ("ということになりますので", "だから"),
    ("させていただく", "する"),
)


def _strip_markdown_prefix(line: str) -> str:
    """見出しや箇条書きの先頭記号を落とす。"""
    stripped = line.lstrip()
    if not stripped:
        return ""

    if stripped.startswith("```"):
        return ""

    if stripped.startswith("|"):
        return ""

    if stripped[0] == "#":
        stripped = stripped.lstrip("#").strip()
    elif stripped.startswith(("- ", "* ", "+ ", "> ")):
        stripped = stripped[2:].strip()
    elif len(stripped) >= 3 and stripped[0].isdigit() and stripped[1:3] in {". ", ") "}:
        stripped = stripped[3:].strip()

    return stripped


def first_meaningful_line(text: str) -> str:
    """コードブロックを避けて最初の意味のある行を返す。"""
    in_code_block = False

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        line = _normalize_line(line)
        if line:
            return line

    return ""


def compact_line(text: str, max_length: int) -> str:
    """余分な言い回しと空白を削って短い一文に整える。"""
    value = _normalize_line(text)
    if not value:
        return ""

    if len(value) > max_length:
        return value[:max_length].rstrip() + "..."

    return value


def _normalize_line(text: str) -> str:
    """行を圧縮用に正規化する。"""
    value = " ".join(text.split())
    if not value:
        return ""

    value = _strip_markdown_prefix(value)
    if not value:
        return ""

    for phrase in _LEADING_PHRASES:
        if value.startswith(phrase):
            value = value[len(phrase) :].lstrip()

    for phrase in _FILLER_PHRASES:
        value = value.replace(phrase, "")

    for source, target in _COMPACTION_REPLACEMENTS:
        value = value.replace(source, target)

    return " ".join(value.split()).strip(" \t。.!?！？、,")
