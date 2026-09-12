"""通知・引き継ぎ本文向けの軽量テキスト圧縮ユーティリティ。

mem/handoff.py の引き継ぎ本文圧縮で使用する。AI 応答本体の圧縮には使われない
（フック制約上、事後圧縮は不可能）。
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


def remove_filler_phrases(value: str) -> str:
    """埋め草表現を位置に関わらず削除する。

    本モジュールの変換のうち、**行の途中から文字を消す唯一の処理**である。
    ``_LEADING_PHRASES`` は行頭のみ、``_COMPACTION_REPLACEMENTS`` は別語句を
    挿入するため前後の文字が隣接せず、空白の畳み込みは空白 1 個を必ず残し、
    末尾の記号除去は端だけを見る。つまり「削除で前後が接着する」経路はここ
    しか無い。

    公開しているのは ``mem/tag_stripping`` がこの接着を検査するためである。
    無害化（escape）の後段でこの削除が走ると ``<system-まあreminder>`` が
    ``<system-reminder>`` へ組み上がり、escape を最後に置いた不変条件が
    パイプライン単位で破れる。検査側が同じ語彙を写経すると片方だけ更新されて
    静かに破れるため、語彙ではなく関数を共有する。

    Args:
        value: 削除前のテキスト。

    Returns:
        埋め草表現を取り除いたテキスト。

    Raises:
        例外は発生しません。
    """
    for phrase in _FILLER_PHRASES:
        value = value.replace(phrase, "")
    return value


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

    value = remove_filler_phrases(value)

    for source, target in _COMPACTION_REPLACEMENTS:
        value = value.replace(source, target)

    return " ".join(value.split()).strip(" \t。.!?！？、,")
