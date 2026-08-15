"""<private> 等のタグをストリップする"""

from __future__ import annotations

import re

# ストリップ対象タグ（大文字小文字区別なし）
_TAGS = (
    "private",
    "mem-context",
    "bluecore-memory",
    "system_instruction",
    "system-instruction",
)

# ReDoS 保護: タグ出現回数の上限
_MAX_TAG_COUNT = 100

# 事前コンパイル済みパターン（開始〜終了タグとその中身を除去）
_PATTERNS = [
    re.compile(
        rf"<{tag}[^>]*>.*?</{tag}>",
        re.DOTALL | re.IGNORECASE,
    )
    for tag in _TAGS
]

# 開始タグと対応しない孤立した閉じタグ用パターン。信頼境界マーカー
# （例: <bluecore-memory>）を早期終端させたように見せかける偽装閉じタグ
# 攻撃を防ぐため、ペア除去後に残った閉じタグ単体も無条件で除去する。
# `.*?` を含まない固定パターンのため ReDoS リスクが無く、_MAX_TAG_COUNT
# ガードは不要（ガード自体が発動してペア除去がスキップされた場合でも、
# この孤立タグ除去は独立して適用される）。
_ORPHAN_CLOSE_PATTERNS = [re.compile(rf"</{tag}\s*>", re.IGNORECASE) for tag in _TAGS]


def strip_tags(text: str) -> str:
    """対象タグとその中身を除去し、連続空行を詰める。

    開始・終了が対になったタグはその中身ごと除去する。加えて、開始タグと
    対応しない孤立した閉じタグ（例: 本文中に紛れ込んだ `</bluecore-memory>`
    単体）も、信頼境界マーカーの偽装終端に使われうるため併せて除去する。
    """
    if not text:
        return text

    for pattern in _PATTERNS:
        if len(pattern.findall(text)) > _MAX_TAG_COUNT:
            continue
        text = pattern.sub("", text)

    for orphan_pattern in _ORPHAN_CLOSE_PATTERNS:
        text = orphan_pattern.sub("", text)

    return re.sub(r"\n{3,}", "\n\n", text).strip()
