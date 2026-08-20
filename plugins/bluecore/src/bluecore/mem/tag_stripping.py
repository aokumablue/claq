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

# 閉じタグと対応しない孤立した開始タグ用パターン（H-07）。ペア除去
# （_PATTERNS）は対になった開始〜終了しか除去できないため、閉じタグを
# 伴わない偽装開始タグ（例: 本文中に紛れ込んだ `<bluecore-memory>` 単体。
# それ以降のテキストが実際の信頼境界ブロック内に見えてしまう）が残存する。
# `_ORPHAN_CLOSE_PATTERNS` と対称に、属性付き開始タグ（`<tag attr="x">`）も
# 含めて対応タグの有無を lookahead せず無条件で除去する。`.*?` を含まない
# 固定パターンのため ReDoS リスクが無く、_MAX_TAG_COUNT ガードは不要
# （ガード自体が発動してペア除去がスキップされた場合でも、この孤立タグ
# 除去は独立して適用される）。
_ORPHAN_OPEN_PATTERNS = [re.compile(rf"<{tag}[^>]*>", re.IGNORECASE) for tag in _TAGS]


def strip_tags(text: str) -> str:
    """対象タグとその中身を除去し、連続空行を詰める。

    開始・終了が対になったタグはその中身ごと除去する。加えて、開始タグと
    対応しない孤立した閉じタグ（例: 本文中に紛れ込んだ `</bluecore-memory>`
    単体）、および閉じタグと対応しない孤立した開始タグ（例: `<bluecore-memory>`
    単体。属性付きも含む）も、信頼境界マーカーの偽装に使われうるため
    併せて除去する（H-07）。
    """
    if not text:
        return text

    for pattern in _PATTERNS:
        if len(pattern.findall(text)) > _MAX_TAG_COUNT:
            continue
        text = pattern.sub("", text)

    for orphan_pattern in _ORPHAN_CLOSE_PATTERNS:
        text = orphan_pattern.sub("", text)

    for orphan_pattern in _ORPHAN_OPEN_PATTERNS:
        text = orphan_pattern.sub("", text)

    return re.sub(r"\n{3,}", "\n\n", text).strip()
