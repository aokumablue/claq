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

# 診断側（ci/scan_scaffold_drift.py）が「既知タグ」を組み立てるための公開名。
STRIPPED_TAGS = _TAGS

# 入れ子除去の最大反復回数。否定先読みは同種の開始タグを跨がないため、
# 入れ子は内側から 1 段ずつ落ちる。1 回の sub では外側の中身が残るため
# 変化が無くなるまで回す（`<private>SECRET<private>x</private>SECRET</private>`
# の SECRET を残さない）。現実の入れ子はごく浅く、上限は暴走防止のみ。
_MAX_NESTING_PASSES = 20

# タグの属性部に許す最大文字数。`[^>]*` を無界にすると、`>` を 1 個も含まない
# 入力（`"<private " * N`）で各開始位置が末尾まで走査して二次オーダーになる。
# 実在の属性がこの長さを超える
# ことはなく、超えた時点でタグとして扱わない。
_MAX_TAG_ATTR_CHARS = 512
_TAG_ATTRS = rf"[^>]{{0,{_MAX_TAG_ATTR_CHARS}}}"

# 事前コンパイル済みパターン（開始〜終了タグとその中身を除去）。
#
# 中身は「同種の開始タグを含まない任意の文字列」に限る。裸の `.*?` だと、
# 閉じタグを伴わない開始タグが並ぶ入力で各開始位置が末尾まで走査して
# 二次オーダーになる（実測: `'<private>' * N` が N=4000 で 0.62 秒、
# 8000 で 2.47 秒、16000 で 9.93 秒 — 入力 2 倍で 4 倍）。属性部の有界化
# （`_TAG_ATTRS`）が塞ぐのは `>` を含まない入力だけで、この経路は別物。
# `strip_tags` は SessionStart の `mem context`（timeout 60 秒）が知識カードの
# title/body に対して毎回呼ぶため、ここの複雑度はセッション開始の遅延になる。
# あわせて、対を成さない開始タグから後続ブロックの閉じタグまで貫通して
# 間のテキストを巻き込む挙動も消える。
_PATTERNS = [
    re.compile(
        rf"<{tag}{_TAG_ATTRS}>(?:(?!<{tag}[\s/>])[\s\S])*?</{tag}\s*>",
        re.IGNORECASE,
    )
    for tag in _TAGS
]

# 開始タグと対応しない孤立した閉じタグ用パターン。信頼境界マーカー
# （例: <bluecore-memory>）を早期終端させたように見せかける偽装閉じタグ
# 攻撃を防ぐため、ペア除去後に残った閉じタグ単体も無条件で除去する。
# `.*?` を含まない固定パターンのため走査は線形。ペア除去とは独立に適用され、
# ペア除去が取りこぼした片側だけのタグを必ず落とす。
_ORPHAN_CLOSE_PATTERNS = [re.compile(rf"</{tag}\s*>", re.IGNORECASE) for tag in _TAGS]

# 閉じタグと対応しない孤立した開始タグ用パターン（H-07）。ペア除去
# （_PATTERNS）は対になった開始〜終了しか除去できないため、閉じタグを
# 伴わない偽装開始タグ（例: 本文中に紛れ込んだ `<bluecore-memory>` 単体。
# それ以降のテキストが実際の信頼境界ブロック内に見えてしまう）が残存する。
# `_ORPHAN_CLOSE_PATTERNS` と対称に、属性付き開始タグ（`<tag attr="x">`）も
# 含めて対応タグの有無を lookahead せず無条件で除去する。`.*?` を含まない
# 固定パターンのため走査は線形。ペア除去とは独立に適用される。
_ORPHAN_OPEN_PATTERNS = [re.compile(rf"<{tag}{_TAG_ATTRS}>", re.IGNORECASE) for tag in _TAGS]


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
        for _ in range(_MAX_NESTING_PASSES):
            stripped = pattern.sub("", text)
            if stripped == text:
                break
            text = stripped

    for orphan_pattern in _ORPHAN_CLOSE_PATTERNS:
        text = orphan_pattern.sub("", text)

    for orphan_pattern in _ORPHAN_OPEN_PATTERNS:
        text = orphan_pattern.sub("", text)

    return re.sub(r"\n{3,}", "\n\n", text).strip()
