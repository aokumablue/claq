"""応答の整形: 入れ子を保った射影・配列の上限・文字数の予算・自由記述の無害化。

結果の大きさは、モデルが読む text（structuredContent を詰めた JSON。guard.py が
詰める）の文字数で測る。トークナイザはモデルごとに違い、サーバはどのモデルが
読むかを知らないため。
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from .errors import FacadeError

MORE_SUFFIX = "_more"
# 取得（*_get）の結果が予算を超えて summary に落としたときに、notes でモデルに伝える文面
FALLBACK_NOTE = "結果が大きいため summary に落としました。必要な項目だけを fields で指定してください。"
_TOO_LARGE_FOR_SUMMARY = "この結果は summary でも上限を超えるため返せません。検索ツールの要約で確かめてください。"
# 一覧（*_search）の件数を予算に合わせて減らしたときに、notes でモデルに伝える文面
REDUCED_NOTE = "結果が大きいため件数を減らしました。続きは next_cursor で取得できます。"
_TOO_LARGE_FOR_ONE = "1 件でも結果が大きすぎて返せません。fields を減らしてください。"

# 射影の葉に許す値（JSON のスカラー）。中間の節に許すのは Mapping と list と null だけ
_JSON_SCALARS = (str, int, float, bool, type(None))

# 端末の制御列。CSI（ESC [ と 8 ビットの U+009B）は引数ごと、OSC（ESC ] と 8 ビットの U+009D）は
# 終端（7 ビットは BEL か ESC \、8 ビットは BEL か U+009C）まで除き、終端が無ければ末尾まで除く。
# 残りの ESC + 1 文字の列も除く。
_ESCAPE_SEQUENCES = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x9b[0-?]*[ -/]*[@-~]"
    r"|\x1b\].*?(?:\x07|\x1b\\|\Z)"
    r"|\x9d.*?(?:\x07|\x9c|\Z)"
    r"|\x1b[@-Z\\-_]",
    re.DOTALL,
)
# 制御文字（Cc）・書式文字（Cf: ゼロ幅・双方向制御・Tag）・私用文字（Co）・サロゲート（Cs）・未割り当て（Cn）
_HIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cs", "Cn"})
_KEPT_CONTROLS = frozenset("\n\t")
# 区分では拾えないが表示されない文字: 異体字セレクタ（Mn）とハングルの埋め字（Lo）
_HIDDEN_LETTERS = re.compile(r"[\ufe00-\ufe0f\U000e0100-\U000e01ef\u115f\u1160\u3164\uffa0]")


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
    """ドット区切りのパスを木にする。葉は None で表す。

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


def _is_scalar_tree(value: Any) -> bool:
    """値が JSON のスカラーか、JSON のスカラーだけの（入れ子の）配列か。

    Args:
        value: 調べる値。

    Returns:
        葉として返してよい値なら True。
    """
    if isinstance(value, list):
        return all(_is_scalar_tree(element) for element in value)
    return isinstance(value, _JSON_SCALARS)


def _select(value: Any, tree: dict[str, Any] | None, array_limit: int) -> Any:
    """木で指定された部分だけを、入れ子の構造を保って取り出す。

    許す値を並べて判定する（許可リスト）。葉は JSON のスカラーかその配列、中間の節は
    object・配列・null だけを許し、それ以外（tuple・dataclass・モデル・スカラー）は失敗にする。

    Args:
        value: 取り出し元の値。
        tree: パスの木（None なら葉。値はスカラーかスカラーの配列）。
        array_limit: 配列ごとに残す件数。

    Returns:
        取り出した値。中間の節が null（値が無い）なら null。

    Raises:
        ValueError: 葉の値が JSON のスカラーかその配列でないとき、または中間の値が object・配列・null のどれでもないとき。
    """
    if tree is None:
        if not _is_scalar_tree(value):
            # 部分木を丸ごと返すと、区分の違う子（個人情報など）が検査なしに出る
            raise ValueError(
                "葉の値が JSON のスカラーかその配列ではありません（object・tuple・dataclass など）。カタログの葉はスカラーにする"
            )
        return cap_arrays(value, array_limit=array_limit)
    if value is None:
        return None
    if isinstance(value, list):
        return [_select(element, tree, array_limit) for element in value[:array_limit]]
    if not isinstance(value, Mapping):
        # 祖先のスカラーや入れ物を返すと、カタログの葉ではない値が区分の検査なしに出る
        raise ValueError(
            "中間の値が object・配列・null のどれでもありません。正規ドメインモデルは dict・list・JSON のスカラーで組む"
        )
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
    パスは無視する（パスの妥当性は catalog で先に検証する）。文書は dict・list・JSON の
    スカラーで組まれている前提で、葉が JSON のスカラーかその配列でない、または中間の値が
    object・配列・null のどれでもないなら、返さずに失敗する（fail closed）。

    Args:
        document: 正規ドメインモデルの文書。
        paths: 取り出すパス（例: `shipping.address.city`, `items.sku`）。
        array_limit: 配列ごとに残す件数。

    Returns:
        取り出した部分。

    Raises:
        ValueError: 文書の形がカタログと食い違うとき（葉が JSON のスカラーかその配列でない、
            中間の値が object・配列・null のどれでもない）。
    """
    return _select(document, _path_tree(paths), array_limit)


def fit_items[T, R](
    items: Sequence[T], build: Callable[[list[T]], R], measure: Callable[[R], int], *, max_chars: int
) -> int:
    """`build(先頭 n 件)` の大きさが `max_chars` 文字に収まる最大の n を返す。

    Args:
        items: 一覧の要素。
        build: 要素の一覧から結果全体を組み立てる関数。
        measure: 結果全体の文字数（モデルが読む text の長さ）を返す関数。
        max_chars: 結果の文字数の上限。

    Returns:
        収まる件数（0 なら 1 件も収まらない）。
    """
    low, high = 0, len(items)
    while low < high:
        middle = (low + high + 1) // 2
        if measure(build(list(items[:middle]))) <= max_chars:
            low = middle
        else:
            high = middle - 1
    return low


def fit_page[T, R](
    items: Sequence[T],
    position: Mapping[str, Any],
    upstream_next: str | None,
    *,
    issue: Callable[[dict[str, Any]], str],
    build: Callable[[list[T], str | None, list[str]], R],
    measure: Callable[[R], int],
    max_chars: int,
    empty_note: str,
) -> R:
    """一覧（`*_search`）の 1 ページを予算に収め、続きの位置を cursor に封じる。

    今回のページの候補のうち、予算に収まる先頭の件数だけを返す。残りがあれば同じ上流の
    ページで読み飛ばす件数を増やした位置を、無ければ上流の次のページの先頭を、続きの
    cursor にする。大きさは、発行しうる続きの cursor のうち最も長いものと、最も長い
    notes で見積もるので、返す結果は必ず予算に収まる。

    上流の次のページトークンを cursor に封じられない（`issue` が失敗する）ときは、今の
    ページが収まっても全体を失敗にする（続きを取れない結果を返さない）。

    Args:
        items: 今回のページの候補（cursor の skip を読み飛ばした後）。
        position: 今回のページの位置（`page` と `skip`）。
        upstream_next: 上流の次のページトークン。無ければ None。
        issue: 位置から続きの cursor を発行する関数（主体と検索条件を結びつけたもの）。
        build: （返す候補、続きの cursor、notes）から結果全体を作る関数。
        measure: 結果全体の文字数（モデルが読む text の長さ）を返す関数。
        max_chars: 結果全体の文字数の上限。
        empty_note: 候補が 0 件で続きも無いときに、notes でモデルに伝える文面（条件の緩め方）。

    Returns:
        結果全体。

    Raises:
        FacadeError: 1 件でも予算を超えるとき、または `issue` が続きの cursor を発行できないとき。
    """
    # 発行しうる続きの位置（同じページの読み飛ばしと、上流の次のページの先頭）だけで見積もる
    candidates = [{"page": position["page"], "skip": position["skip"] + len(items)}]
    if upstream_next:
        candidates.append({"page": upstream_next, "skip": 0})
    longest_cursor = max((issue(candidate) for candidate in candidates), key=len)
    longest_notes = [max(REDUCED_NOTE, empty_note, key=len)]
    count = fit_items(items, lambda head: build(head, longest_cursor, longest_notes), measure, max_chars=max_chars)
    if items and count == 0:
        raise FacadeError(_TOO_LARGE_FOR_ONE)
    if count < len(items):
        next_position: dict[str, Any] | None = {"page": position["page"], "skip": position["skip"] + count}
        notes = [REDUCED_NOTE]
    else:
        next_position = {"page": upstream_next, "skip": 0} if upstream_next else None
        notes = [] if items or next_position else [empty_note]
    next_cursor = issue(next_position) if next_position else None
    return build(list(items[:count]), next_cursor, notes)


def fit_view[T](
    build: Callable[[Sequence[str], list[str]], T],
    measure: Callable[[T], int],
    *,
    paths: Sequence[str],
    fallback_paths: Sequence[str],
    max_chars: int,
) -> tuple[T, list[str]]:
    """取得（`*_get`）の結果を予算に収める。

    結果全体（notes・partial を含む）で測り、超えたら fallback（summary ビュー）に落として
    `FALLBACK_NOTE` を notes に足す。それでも超えたら失敗にする。

    Args:
        build: （返す項目のパス、notes）から結果全体を作る関数。
        measure: 結果全体の文字数（モデルが読む text の長さ）を返す関数。
        paths: 返したい項目のパス（ビューと fields）。
        fallback_paths: 予算を超えたときに落とす先のパス（summary ビュー）。
        max_chars: 結果全体の文字数の上限。

    Returns:
        （結果、実際に返した項目のパス）。監査ログには実際に返したパスを渡す。

    Raises:
        FacadeError: fallback でも上限を超えるとき。
    """
    result = build(paths, [])
    if measure(result) <= max_chars:
        return result, list(paths)
    result = build(fallback_paths, [FALLBACK_NOTE])
    if measure(result) > max_chars:
        raise FacadeError(_TOO_LARGE_FOR_SUMMARY)
    return result, list(fallback_paths)


def _is_hidden(char: str) -> bool:
    """表示されない、または表示と中身を食い違わせる区分の文字か（改行とタブは残す）。

    Args:
        char: 調べる 1 文字。

    Returns:
        除く文字なら True。
    """
    return char not in _KEPT_CONTROLS and unicodedata.category(char) in _HIDDEN_CATEGORIES


def clean_text(value: str, *, max_chars: int) -> str:
    """上流から来た文字列を、データとして安全な文字列にする。

    次の順で整える。

    1. CRLF を LF に揃える。
    2. 端末の制御列（CSI と OSC。7 ビットの ESC の形と、8 ビットの C1 の形）を列ごと除く。
       OSC は終端まで、終端が無ければ末尾まで除く。文字ごとに除くと、列の引数（`31m` など）が残る。
    3. 制御文字（Cc。改行とタブは残し、単独の CR は除く）・書式文字（Cf: ゼロ幅・双方向制御・Tag）・
       私用文字（Co）・サロゲート（Cs）・未割り当て（Cn）・異体字セレクタ・ハングルの埋め字を除く。
    4. 除いた後で `max_chars` 文字を超えれば切り詰め、末尾に「…」を付ける。

    指示文が書かれていても、ここでは消さない。データとして専用の項目に入れ、サーバが書く
    文章に連結しないことで扱う。

    Args:
        value: 上流から来た文字列。
        max_chars: 残す最大文字数。

    Returns:
        無害化した文字列。
    """
    text = _ESCAPE_SEQUENCES.sub("", value.replace("\r\n", "\n"))
    text = _HIDDEN_LETTERS.sub("", "".join(char for char in text if not _is_hidden(char)))
    return text if len(text) <= max_chars else text[:max_chars] + "…"
