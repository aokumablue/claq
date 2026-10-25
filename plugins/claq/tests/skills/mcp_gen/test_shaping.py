"""facade/shaping.py（応答の整形と、取得の結果を予算に収める判定）のテスト。"""

from __future__ import annotations

import types
from dataclasses import dataclass
from typing import Any

import pytest


@dataclass
class _Address:
    """正規ドメインモデルに入れてはいけない入れ物（dataclass）の見本。"""

    city: str
    postal_code: str


_ORDER = {
    "order_id": "o1",
    "shipping": {"address": {"city": "大阪市", "postal_code": "5300001"}},
    "items": [{"sku": f"S{i}", "quantity": i, "price": {"amount": i}} for i in range(12)],
    "status": "open",
}


class TestShaping:
    """応答の整形。"""

    def test_compact_json_keeps_japanese(self, facade: types.SimpleNamespace) -> None:
        """日本語をエスケープせず、空白を入れない。"""
        assert facade.shaping.compact_json({"a": "田中", "b": [1, 2]}) == '{"a":"田中","b":[1,2]}'

    @pytest.mark.parametrize(
        ("document", "paths", "array_limit", "expected"),
        [
            (
                _ORDER,
                ["shipping.address.city", "items.sku"],
                2,
                {"shipping": {"address": {"city": "大阪市"}}, "items": [{"sku": "S0"}, {"sku": "S1"}], "items_more": {"total": 12, "returned": 2}},
            ),
            (
                {"tags": ["a", "b", "c"], "grid": [[1, 2, 3], [4]]},
                ["tags", "grid"],
                2,
                {"tags": ["a", "b"], "tags_more": {"total": 3, "returned": 2}, "grid": [[1, 2], [4]]},
            ),
            (_ORDER, ["nope.x", "order_id"], 5, {"order_id": "o1"}),
            ({"shipping": None, "items": [{"sku": "a"}, None]}, ["shipping.address.city", "items.sku"], 5, {"shipping": None, "items": [{"sku": "a"}, None]}),
            ({"grid": [[{"v": 1}, {"v": 2}, {"v": 3}], [{"v": 4}]]}, ["grid.v"], 2, {"grid": [[{"v": 1}, {"v": 2}], [{"v": 4}]]}),
        ],
        ids=["structure-kept-arrays-capped", "scalar-list-leaf-capped", "missing-path-ignored", "null-ancestor-is-null", "nested-lists-capped"],
    )
    def test_project(
        self, facade: types.SimpleNamespace, document: dict[str, Any], paths: list[str], array_limit: int, expected: dict[str, Any]
    ) -> None:
        """指定したパスだけを入れ子のまま返す。配列（スカラーの配列の葉・入れ子の配列を含む）は上限で切って総数を添え、存在しないパスは無視し（妥当性は catalog が先に検証する）、祖先が null ならその位置に null を返す（「取得していない」の省略と区別する）。"""
        assert facade.shaping.project(document, paths, array_limit=array_limit) == expected

    @pytest.mark.parametrize(
        ("document", "paths"),
        [
            (_ORDER, ["shipping"]),
            (_ORDER, ["shipping.address"]),
            (_ORDER, ["items"]),
            (_ORDER, ["items", "items.sku"]),
            (_ORDER, ["items.sku", "items"]),
            (_ORDER, ["items.price"]),
            ({"grid": [[1], [{"v": 1}]]}, ["grid"]),
            ({"leaf": ({"sku": "a"},)}, ["leaf"]),
            ({"leaf": ("a", "b")}, ["leaf"]),
            ({"leaf": _Address("大阪市", "5300001")}, ["leaf"]),
            ({"leaf": [("a",)]}, ["leaf"]),
            ({"leaf": {"a"}}, ["leaf"]),
            ({"leaf": b"a"}, ["leaf"]),
        ],
        ids=[
            "object",
            "nested-object",
            "object-array",
            "ancestor-absorbs-descendant",
            "ancestor-after-descendant",
            "object-under-array",
            "object-deep-in-nested-array",
            "tuple-of-object",
            "tuple",
            "dataclass",
            "array-of-tuple",
            "set",
            "bytes",
        ],
    )
    def test_project_non_scalar_leaf_raises(self, facade: types.SimpleNamespace, document: dict[str, Any], paths: list[str]) -> None:
        """葉の値が JSON のスカラーかその配列でなければ（object・入れ子の配列の奥の object・tuple・dataclass・set・bytes）、部分木を丸ごと返さずに失敗する（祖先が子孫を吸収しても同じ）。"""
        with pytest.raises(ValueError, match="object"):
            facade.shaping.project(document, paths, array_limit=5)

    @pytest.mark.parametrize(
        ("document", "path"),
        [
            ({"items": ({"sku": "a", "postal_code": "5300001"},)}, "items.sku"),
            ({"shipping": _Address("大阪市", "5300001")}, "shipping.city"),
            ({"status": "open"}, "status.deeper"),
            ({"items": [{"sku": "a"}, "stray"]}, "items.sku"),
        ],
    )
    def test_project_non_mapping_ancestor_raises(
        self, facade: types.SimpleNamespace, document: dict[str, Any], path: str
    ) -> None:
        """祖先の値が object・配列・null のどれでもない（tuple・dataclass・スカラー）なら、丸ごと返さずに失敗する。"""
        with pytest.raises(ValueError, match="中間"):
            facade.shaping.project(document, [path], array_limit=5)

    def test_fit_items(self, facade: types.SimpleNamespace) -> None:
        """予算に収まる最大の件数を返す。"""
        items = ["x" * 10] * 10

        def build(chosen: list[str]) -> dict[str, Any]:
            """一覧を包んだ結果を作る。"""
            return {"items": chosen}

        def measure(result: dict[str, Any]) -> int:
            """結果を詰めた JSON の文字数。"""
            return len(facade.shaping.compact_json(result))

        size = measure(build(items[:4]))
        assert facade.shaping.fit_items(items, build, measure, max_chars=size) == 4
        assert facade.shaping.fit_items(items, build, measure, max_chars=10_000) == 10
        assert facade.shaping.fit_items(items, build, measure, max_chars=5) == 0
        assert facade.shaping.fit_items([], build, measure, max_chars=100) == 0

    @pytest.mark.parametrize(
        ("raw", "max_chars", "expected"),
        [
            ("至急\x1b[31m 対応\u200b\u202eお願い\x07\n\tします\x1bM", 100, "至急 対応お願い\n\tします"),  # 改行とタブは残す
            ("あいうえお", 3, "あいう…"),  # 長すぎれば切り詰める
            ("a\u200bb\u200bc", 3, "abc"),  # 切り詰めは除いた後の文字数で行う（除く文字が上限を食わない）
            ("a\U000e0041b", 100, "ab"),  # Tag 文字（Cf）
            ("a\u2066b\u2069c", 100, "abc"),  # 双方向の isolate（Cf）
            ("a\x9b31mb", 100, "ab"),  # 8 ビットの CSI 列は引数ごと除く
            ("a\x9d0;title\x07b", 100, "ab"),  # 8 ビットの OSC 列（BEL で終端）
            ("a\x9d0;title\x9cb", 100, "ab"),  # 8 ビットの OSC 列（ST で終端）
            ("a\x9d0;title", 100, "a"),  # 終端の無い 8 ビットの OSC 列は末尾まで
            ("a\x1b]0;title\x07b", 100, "ab"),  # 7 ビットの OSC 列（BEL で終端）
            ("a\x1b]8;;https://example.com\x1b\\b", 100, "ab"),  # 7 ビットの OSC 列（ESC \ で終端）
            ("a\x1b]0;title", 100, "a"),  # 終端の無い 7 ビットの OSC 列は末尾まで
            ("a\rb", 100, "ab"),  # 単独の CR
            ("a\r\nb", 100, "a\nb"),  # CRLF は LF に揃える
            ("a\ufe0fb\U000e0100c", 100, "abc"),  # 異体字セレクタ
            ("a\u115fb\u1160c\u3164d\uffa0e", 100, "abcde"),  # ハングルの埋め字
            ("a\ue000b", 100, "ab"),  # 私用文字（Co）
            ("a\uffffb", 100, "ab"),  # 非文字（Cn）
            ("a\ud800b", 100, "ab"),  # 対になっていないサロゲート（Cs）
            ("a\u2028b\u2029c", 100, "a\nb\nc"),  # 行区切り（Zl）・段落区切り（Zp）は改行に畳む
            ("a\u034fb\u2800c", 100, "abc"),  # 結合書記素ジョイナ・点字の空白
            ("a\u17b4b\u17b5c", 100, "abc"),  # クメールの不可視母音
            ("a\u180bb\u180cc\u180dd\u180fe", 100, "abcde"),  # モンゴルの異体字セレクタ
            ("a\U00016fe4b", 100, "ab"),  # 契丹の埋め字
        ],
    )
    def test_clean_text(self, facade: types.SimpleNamespace, raw: str, max_chars: int, expected: str) -> None:
        """表示と中身を食い違わせる制御列と不可視文字を除き（ESC の列は引数ごと、文字は 1 つずつ）、除いた後で長すぎれば切り詰める。"""
        assert facade.shaping.clean_text(raw, max_chars=max_chars) == expected


def _view_result(paths: Any, notes: Any) -> dict[str, Any]:
    """パスと notes から、取得の結果全体の代わりを作る（項目ごとに 100 文字の値。notes の 1 文より大きくする）。"""
    return {"order": dict.fromkeys(paths, "x" * 100), "notes": list(notes)}


class TestFitView:
    """取得（*_get）の結果を予算に収める判定（全体で測り、超えたら summary に落とし、それでも超えたら失敗）。"""

    _PATHS = ["a", "b", "c"]

    @staticmethod
    def _fit(facade: types.SimpleNamespace, max_chars: int) -> tuple[Any, list[str]]:
        """見本のパス（fallback は a だけ）で fit_view を呼ぶ。"""
        return facade.shaping.fit_view(
            _view_result,
            lambda result: len(facade.shaping.compact_json(result)),
            paths=TestFitView._PATHS,
            fallback_paths=["a"],
            max_chars=max_chars,
        )

    def test_fit_view_within_budget_keeps_paths(self, facade: types.SimpleNamespace) -> None:
        """結果全体が予算ちょうどまでなら、指定のパスのまま notes を足さずに返し、返したパスも添える。"""
        whole = _view_result(self._PATHS, [])
        assert self._fit(facade, len(facade.shaping.compact_json(whole))) == (whole, self._PATHS)

    def test_fit_view_over_budget_falls_back(self, facade: types.SimpleNamespace) -> None:
        """予算を 1 文字でも超えたら fallback（summary）のパスに落とし、notes で fields の絞り方を伝え、落とした後のパスを返す。"""
        size = len(facade.shaping.compact_json(_view_result(self._PATHS, [])))
        result, paths = self._fit(facade, size - 1)
        assert (result, paths) == (_view_result(["a"], [facade.shaping.FALLBACK_NOTE]), ["a"])

    def test_fit_view_fallback_over_budget_raises(self, facade: types.SimpleNamespace) -> None:
        """fallback でも予算を超えるなら、返さずに FacadeError にする。"""
        with pytest.raises(facade.errors.FacadeError, match="summary でも"):
            self._fit(facade, 5)


def _page_result(items: list[str], next_cursor: str | None, notes: list[str]) -> dict[str, Any]:
    """一覧の結果全体の代わりを作る（続きが無ければ next_cursor を出さない）。"""
    result: dict[str, Any] = {"items": items, "has_more": next_cursor is not None, "notes": notes}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def _readable_cursor(position: dict[str, Any]) -> str:
    """位置をそのまま読める `cursor:<page>:<skip>` の形にする（発行した位置をテストで確かめるため）。"""
    return f"cursor:{position['page']}:{position['skip']}"


class TestFitPage:
    """一覧（*_search）の 1 ページを予算に収め、続きの位置を cursor に封じる判定。"""

    _ITEMS = [f"item-{index:02d}-" + "x" * 40 for index in range(6)]
    _EMPTY = "該当する候補はありません。条件を緩めてください。"

    def _fit(
        self,
        facade: types.SimpleNamespace,
        items: list[str],
        *,
        max_chars: int = 10_000,
        upstream_next: str | None = "p2",
        issue: Any = _readable_cursor,
    ) -> Any:
        """今回のページの位置を (p1, skip 3) として fit_page を呼ぶ。"""
        return facade.shaping.fit_page(
            items,
            {"page": "p1", "skip": 3},
            upstream_next,
            issue=issue,
            build=_page_result,
            measure=lambda result: len(facade.shaping.compact_json(result)),
            max_chars=max_chars,
            empty_note=self._EMPTY,
        )

    @pytest.mark.parametrize(
        ("items", "upstream_next", "expected"),
        [
            (_ITEMS, "p2", _page_result(_ITEMS, "cursor:p2:0", [])),
            (_ITEMS, None, _page_result(_ITEMS, None, [])),
            ([], None, _page_result([], None, [_EMPTY])),
            ([], "p2", _page_result([], "cursor:p2:0", [])),
        ],
        ids=["all-fit-upstream-next", "all-fit-no-upstream-next", "empty-no-upstream-next", "empty-upstream-next"],
    )
    def test_within_budget(
        self, facade: types.SimpleNamespace, items: list[str], upstream_next: str | None, expected: dict[str, Any]
    ) -> None:
        """全件が収まれば、続きは上流の次のページの先頭にし、上流にも次が無ければ cursor を出さない。条件の緩め方の notes は、候補が 0 件で続きも無いときだけ付ける。"""
        assert self._fit(facade, items, upstream_next=upstream_next) == expected

    def test_over_budget_reduces_and_continues_same_page(self, facade: types.SimpleNamespace) -> None:
        """予算を超えたら収まる件数に減らし、続きは同じ上流のページで読み飛ばす件数を増やした位置にする。"""
        longest_note = max(facade.shaping.REDUCED_NOTE, self._EMPTY, key=len)
        budget = len(facade.shaping.compact_json(_page_result(self._ITEMS[:4], "cursor:p1:9", [longest_note])))
        result = self._fit(facade, self._ITEMS, max_chars=budget)
        assert result == _page_result(self._ITEMS[:4], "cursor:p1:7", [facade.shaping.REDUCED_NOTE])

    @pytest.mark.parametrize(
        ("items", "max_chars", "match"), [(_ITEMS, 20, "1 件でも"), ([], 10, "上限")], ids=["single-item-over-budget", "empty-page-over-budget"]
    )
    def test_over_budget_raises(self, facade: types.SimpleNamespace, items: list[str], max_chars: int, match: str) -> None:
        """1 件でも予算を超えるなら fields を減らすよう求め、0 件でも結果全体（続きの cursor と notes）が超えるなら、空の結果を返さずに失敗する。"""
        with pytest.raises(facade.errors.FacadeError, match=match):
            self._fit(facade, items, max_chars=max_chars)

    def test_unsealable_upstream_token_fails_whole_page(self, facade: types.SimpleNamespace) -> None:
        """上流の次のページトークンを cursor に封じられなければ、今のページが収まっても全体を失敗にする（続きを取れない結果を返さない）。"""

        def refuse_upstream_page(position: dict[str, Any]) -> str:
            """上流の次のページ（p2）だけ発行を断る。"""
            if position["page"] == "p2":
                raise facade.errors.FacadeError("too long")
            return _readable_cursor(position)

        with pytest.raises(facade.errors.FacadeError, match="too long"):
            self._fit(facade, self._ITEMS, issue=refuse_upstream_page)

    def test_empty_page_fits_or_fails(self, facade: types.SimpleNamespace) -> None:
        """候補が 0 件のときも、返した結果は必ず予算に収まる（収まらなければ失敗にする）。"""
        returned = 0
        for budget in range(1, 200, 3):
            for upstream_next in ("p2", None):
                try:
                    result = self._fit(facade, [], max_chars=budget, upstream_next=upstream_next)
                except facade.errors.FacadeError:
                    continue
                returned += 1
                assert len(facade.shaping.compact_json(result)) <= budget, (budget, upstream_next)
        assert returned > 0

    def test_result_always_fits_budget(self, facade: types.SimpleNamespace) -> None:
        """どの予算でも、返した結果全体は予算に収まる（見積もりは実際の cursor と notes 以上）。"""
        longest_note = max(facade.shaping.REDUCED_NOTE, self._EMPTY, key=len)
        smallest = len(facade.shaping.compact_json(_page_result(self._ITEMS[:1], "cursor:p1:9", [longest_note])))
        for budget in range(smallest, smallest + 400, 7):
            for upstream_next in ("p2", None):
                result = self._fit(facade, self._ITEMS, max_chars=budget, upstream_next=upstream_next)
                assert len(facade.shaping.compact_json(result)) <= budget, (budget, upstream_next)
