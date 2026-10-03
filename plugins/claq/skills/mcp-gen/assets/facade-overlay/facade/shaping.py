"""応答の整形: 入れ子を保った射影・配列の上限・文字数の予算・自由記述の無害化。

結果の大きさは、モデルが読む text（structuredContent を詰めた JSON。guard.py が
詰める）の文字数で測る。トークナイザはモデルごとに違い、サーバはどのモデルが
読むかを知らないため。
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any

MORE_SUFFIX = "_more"

# 端末の制御列・制御文字・ゼロ幅文字・双方向制御文字（表示と中身を食い違わせる）
_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
_INVISIBLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁠-⁤﻿]")


def compact_json(value: Any) -> str:
    """モデルが読む形（日本語をそのまま残した詰めた JSON）にする。

    Args:
        value: JSON にできる値。

    Returns:
        JSON 文字列。
    """
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def cap_arrays(value: Any, *, array_limit: int) -> Any:
    """値の中の配列を先頭 `array_limit` 件にし、隣に `<名前>_more` で総数を添える。

    Args:
        value: 整形する値。
        array_limit: 配列ごとに残す件数。

    Returns:
        整形した値（元の値は変更しない）。
    """
    if isinstance(value, Mapping):
        capped: dict[str, Any] = {}
        for key, child in value.items():
            if isinstance(child, list):
                capped[key] = [cap_arrays(element, array_limit=array_limit) for element in child[:array_limit]]
                if len(child) > array_limit:
                    capped[f"{key}{MORE_SUFFIX}"] = {"total": len(child), "returned": array_limit}
            else:
                capped[key] = cap_arrays(child, array_limit=array_limit)
        return capped
    if isinstance(value, list):
        return [cap_arrays(element, array_limit=array_limit) for element in value[:array_limit]]
    return value


def _path_tree(paths: Sequence[str]) -> dict[str, Any]:
    """ドット区切りのパスを木にする。葉（その下を全て取る）は None で表す。

    Args:
        paths: `shipping.address.city` 形式のパス。

    Returns:
        パスの木。祖先が葉なら子孫のパスは吸収される。
    """
    tree: dict[str, Any] = {}
    for path in paths:
        node = tree
        parts = path.split(".")
        for index, part in enumerate(parts):
            if index == len(parts) - 1:
                node[part] = None
            elif node.get(part, {}) is None:
                break
            else:
                node = node.setdefault(part, {})
    return tree


def _select(value: Any, tree: dict[str, Any] | None, array_limit: int) -> Any:
    """木で指定された部分だけを、入れ子の構造を保って取り出す。

    Args:
        value: 取り出し元の値。
        tree: パスの木（None なら値全体）。
        array_limit: 配列ごとに残す件数。

    Returns:
        取り出した値。
    """
    if tree is None:
        return cap_arrays(value, array_limit=array_limit)
    if isinstance(value, list):
        return [_select(element, tree, array_limit) for element in value[:array_limit]]
    if not isinstance(value, Mapping):
        return value
    selected: dict[str, Any] = {}
    for key, subtree in tree.items():
        if key not in value:
            continue
        child = value[key]
        if isinstance(child, list):
            selected[key] = [_select(element, subtree, array_limit) for element in child[:array_limit]]
            if len(child) > array_limit:
                selected[f"{key}{MORE_SUFFIX}"] = {"total": len(child), "returned": array_limit}
        else:
            selected[key] = _select(child, subtree, array_limit)
    return selected


def project(document: Mapping[str, Any], paths: Sequence[str], *, array_limit: int) -> dict[str, Any]:
    """指定したパスだけを、入れ子の構造を保ったまま取り出す。

    パスごとの平坦化はしない（配列の中の複数の項目の対応関係が崩れるため）。
    配列は先頭 `array_limit` 件だけを残し、隣に `<名前>_more` を置く。存在しない
    パスは無視する（パスの妥当性は catalog で先に検証する）。

    Args:
        document: 正規ドメインモデルの文書。
        paths: 取り出すパス（例: `shipping.address.city`, `items.sku`）。
        array_limit: 配列ごとに残す件数。

    Returns:
        取り出した部分。
    """
    return _select(document, _path_tree(paths), array_limit)


def fit_items(items: Sequence[Any], build: Callable[[list[Any]], Any], *, max_chars: int) -> int:
    """`build(先頭 n 件)` の JSON が `max_chars` 文字に収まる最大の n を返す。

    Args:
        items: 一覧の要素。
        build: 要素の一覧から結果全体（JSON にできる値）を組み立てる関数。
        max_chars: 結果の文字数の上限。

    Returns:
        収まる件数（0 なら 1 件も収まらない）。
    """
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        if len(compact_json(build(list(items[:middle])))) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return low


def clean_text(value: str, *, max_chars: int) -> str:
    """上流から来た自由記述を、データとして安全な文字列にする。

    端末の制御列・制御文字・ゼロ幅文字・双方向制御文字を取り除き（改行とタブは残す）、
    長すぎれば切り詰めて末尾に「…」を付ける。指示文が書かれていても、ここでは
    消さない。データとして専用の項目に入れ、サーバが書く文章に連結しないことで扱う。

    Args:
        value: 上流から来た文字列。
        max_chars: 残す最大文字数。

    Returns:
        無害化した文字列。
    """
    text = _INVISIBLE.sub("", _ANSI.sub("", value))
    return text if len(text) <= max_chars else text[:max_chars] + "…"
