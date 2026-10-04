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


class TestShaping:
    """応答の整形。"""

    _ORDER = {
        "order_id": "o1",
        "shipping": {"address": {"city": "大阪市", "postal_code": "5300001"}},
        "items": [{"sku": f"S{i}", "quantity": i, "price": {"amount": i}} for i in range(12)],
        "status": "open",
    }

    def test_compact_json_keeps_japanese(self, facade: types.SimpleNamespace) -> None:
        """日本語をエスケープせず、空白を入れない。"""
        assert facade.shaping.compact_json({"a": "田中", "b": [1, 2]}) == '{"a":"田中","b":[1,2]}'

    def test_project_keeps_structure(self, facade: types.SimpleNamespace) -> None:
        """指定したパスだけを入れ子のまま返し、配列は上限と総数を添える。"""
        projected = facade.shaping.project(self._ORDER, ["shipping.address.city", "items.sku"], array_limit=2)
        assert projected == {
            "shipping": {"address": {"city": "大阪市"}},
            "items": [{"sku": "S0"}, {"sku": "S1"}],
            "items_more": {"total": 12, "returned": 2},
        }

    @pytest.mark.parametrize(
        "paths",
        [["shipping"], ["shipping.address"], ["items"], ["items", "items.sku"], ["items.sku", "items"], ["items.price"]],
    )
    def test_project_mapping_leaf_raises(self, facade: types.SimpleNamespace, paths: list[str]) -> None:
        """葉の値が object か object の配列なら、部分木を丸ごと返さずに失敗する（祖先が子孫を吸収しても同じ）。"""
        with pytest.raises(ValueError, match="object"):
            facade.shaping.project(self._ORDER, paths, array_limit=1)

    def test_project_nested_list_with_mapping_leaf_raises(self, facade: types.SimpleNamespace) -> None:
        """入れ子の配列の奥にある object も、葉の値としては返さない。"""
        with pytest.raises(ValueError, match="object"):
            facade.shaping.project({"grid": [[1], [{"v": 1}]]}, ["grid"], array_limit=5)

    def test_project_scalar_list_leaf_is_capped(self, facade: types.SimpleNamespace) -> None:
        """葉の値がスカラーの配列（入れ子を含む）なら、上限で切って総数を添える。"""
        document = {"tags": ["a", "b", "c"], "grid": [[1, 2, 3], [4]]}
        assert facade.shaping.project(document, ["tags", "grid"], array_limit=2) == {
            "tags": ["a", "b"],
            "tags_more": {"total": 3, "returned": 2},
            "grid": [[1, 2], [4]],
        }

    def test_project_ignores_missing_paths(self, facade: types.SimpleNamespace) -> None:
        """存在しないパスは無視する（パスの妥当性は catalog で先に検証する）。"""
        projected = facade.shaping.project(self._ORDER, ["nope.x", "order_id"], array_limit=5)
        assert projected == {"order_id": "o1"}

    def test_project_null_ancestor_is_null(self, facade: types.SimpleNamespace) -> None:
        """祖先が null（値が無い）なら、その位置に null を返す（「取得していない」の省略と区別する）。"""
        document = {"shipping": None, "items": [{"sku": "a"}, None]}
        assert facade.shaping.project(document, ["shipping.address.city", "items.sku"], array_limit=5) == {
            "shipping": None,
            "items": [{"sku": "a"}, None],
        }

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

    @pytest.mark.parametrize(
        "value", [({"sku": "a"},), ("a", "b"), _Address("大阪市", "5300001"), [("a",)], {"a"}, b"a"]
    )
    def test_project_non_json_leaf_raises(self, facade: types.SimpleNamespace, value: Any) -> None:
        """葉の値が JSON のスカラーかその配列でなければ（tuple・dataclass・set・bytes）、返さずに失敗する。"""
        with pytest.raises(ValueError, match="object"):
            facade.shaping.project({"leaf": value}, ["leaf"], array_limit=5)

    def test_project_nested_lists(self, facade: types.SimpleNamespace) -> None:
        """配列の中の配列も上限を守る。"""
        document = {"grid": [[{"v": 1}, {"v": 2}, {"v": 3}], [{"v": 4}]]}
        assert facade.shaping.project(document, ["grid.v"], array_limit=2) == {
            "grid": [[{"v": 1}, {"v": 2}], [{"v": 4}]]
        }

    def test_cap_arrays(self, facade: types.SimpleNamespace) -> None:
        """全ての配列を上限で切り、元の値は変えない。"""
        value = {"a": [1, 2, 3], "b": {"c": [4, 5]}, "d": 1}
        assert facade.shaping.cap_arrays(value, array_limit=2) == {
            "a": [1, 2],
            "a_more": {"total": 3, "returned": 2},
            "b": {"c": [4, 5]},
            "d": 1,
        }
        assert facade.shaping.cap_arrays([[1, 2, 3]], array_limit=2) == [[1, 2]]
        assert value["a"] == [1, 2, 3]

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

    def test_clean_text(self, facade: types.SimpleNamespace) -> None:
        """制御列・不可視文字を除き、改行とタブは残し、長すぎれば切り詰める。"""
        raw = "至急\x1b[31m 対応\u200b\u202eお願い\x07\n\tします\x1bM"
        assert facade.shaping.clean_text(raw, max_chars=100) == "至急 対応お願い\n\tします"
        assert facade.shaping.clean_text("あいうえお", max_chars=3) == "あいう…"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("a\U000e0041b", "ab"),  # Tag 文字（Cf）
            ("a\u2066b\u2069c", "abc"),  # 双方向の isolate（Cf）
            ("a\x9b31mb", "ab"),  # 8 ビットの CSI 列は引数ごと除く
            ("a\x9d0;title\x07b", "ab"),  # 8 ビットの OSC 列（BEL で終端）
            ("a\x9d0;title\x9cb", "ab"),  # 8 ビットの OSC 列（ST で終端）
            ("a\x9d0;title", "a"),  # 終端の無い 8 ビットの OSC 列は末尾まで
            ("a\x1b]0;title\x07b", "ab"),  # 7 ビットの OSC 列（BEL で終端）
            ("a\x1b]8;;https://example.com\x1b\\b", "ab"),  # 7 ビットの OSC 列（ESC \ で終端）
            ("a\x1b]0;title", "a"),  # 終端の無い 7 ビットの OSC 列は末尾まで
            ("a\rb", "ab"),  # 単独の CR
            ("a\r\nb", "a\nb"),  # CRLF は LF に揃える
            ("a\ufe0fb\U000e0100c", "abc"),  # 異体字セレクタ
            ("a\u115fb\u1160c\u3164d\uffa0e", "abcde"),  # ハングルの埋め字
            ("a\ue000b", "ab"),  # 私用文字（Co）
            ("a\uffffb", "ab"),  # 非文字（Cn）
            ("a\ud800b", "ab"),  # 対になっていないサロゲート（Cs）
        ],
    )
    def test_clean_text_removes_hidden_content(self, facade: types.SimpleNamespace, raw: str, expected: str) -> None:
        """表示と中身を食い違わせる列と文字を除く（ESC の列は引数ごと、文字は 1 つずつ）。"""
        assert facade.shaping.clean_text(raw, max_chars=100) == expected

    def test_clean_text_truncates_after_removal(self, facade: types.SimpleNamespace) -> None:
        """切り詰めは除いた後の文字数で行う（除く文字が上限を食わない）。"""
        assert facade.shaping.clean_text("a\u200bb\u200bc", max_chars=3) == "abc"


def _view_result(paths: Any, notes: Any) -> dict[str, Any]:
    """パスと notes から、取得の結果全体の代わりを作る（項目ごとに 100 文字の値。notes の 1 文より大きくする）。

    Args:
        paths: 返す項目のパス。
        notes: 次の行動の提案。

    Returns:
        結果全体（JSON にできる値）。
    """
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
    """一覧の結果全体の代わりを作る（続きが無ければ next_cursor を出さない）。

    Args:
        items: 返す候補。
        next_cursor: 続きの cursor。無ければ None。
        notes: 次の行動の提案。

    Returns:
        結果全体（JSON にできる値）。
    """
    result: dict[str, Any] = {"items": items, "has_more": next_cursor is not None, "notes": notes}
    if next_cursor is not None:
        result["next_cursor"] = next_cursor
    return result


def _readable_cursor(position: dict[str, Any]) -> str:
    """位置をそのまま読める cursor にする（発行した位置をテストで確かめるため）。

    Args:
        position: 続きの位置（page と skip）。

    Returns:
        `cursor:<page>:<skip>` の形の文字列。
    """
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

    def test_fits_all_continues_with_upstream_page(self, facade: types.SimpleNamespace) -> None:
        """全件が収まれば、続きは上流の次のページの先頭にする（notes は付けない）。"""
        assert self._fit(facade, self._ITEMS) == _page_result(self._ITEMS, "cursor:p2:0", [])

    def test_fits_all_without_upstream_next_ends(self, facade: types.SimpleNamespace) -> None:
        """全件が収まり、上流にも次が無ければ、続きの cursor を出さない。"""
        assert self._fit(facade, self._ITEMS, upstream_next=None) == _page_result(self._ITEMS, None, [])

    def test_over_budget_reduces_and_continues_same_page(self, facade: types.SimpleNamespace) -> None:
        """予算を超えたら収まる件数に減らし、続きは同じ上流のページで読み飛ばす件数を増やした位置にする。"""
        longest_note = max(facade.shaping.REDUCED_NOTE, self._EMPTY, key=len)
        budget = len(facade.shaping.compact_json(_page_result(self._ITEMS[:4], "cursor:p1:9", [longest_note])))
        result = self._fit(facade, self._ITEMS, max_chars=budget)
        assert result == _page_result(self._ITEMS[:4], "cursor:p1:7", [facade.shaping.REDUCED_NOTE])

    def test_empty_page_without_continuation_says_so(self, facade: types.SimpleNamespace) -> None:
        """候補が 0 件で続きも無ければ、条件の緩め方を notes で伝える。"""
        assert self._fit(facade, [], upstream_next=None) == _page_result([], None, [self._EMPTY])

    def test_empty_page_with_upstream_next_continues_without_note(self, facade: types.SimpleNamespace) -> None:
        """候補が 0 件でも上流に次があれば、該当なしとは言わずに続きの cursor を返す。"""
        assert self._fit(facade, []) == _page_result([], "cursor:p2:0", [])

    def test_single_item_over_budget_raises(self, facade: types.SimpleNamespace) -> None:
        """1 件でも予算を超えるなら、空の結果を返さずに fields を減らすよう求める失敗にする。"""
        with pytest.raises(facade.errors.FacadeError, match="1 件でも"):
            self._fit(facade, self._ITEMS, max_chars=20)

    def test_unsealable_upstream_token_fails_whole_page(self, facade: types.SimpleNamespace) -> None:
        """上流の次のページトークンを cursor に封じられなければ、今のページが収まっても全体を失敗にする（続きを取れない結果を返さない）。"""

        def refuse_upstream_page(position: dict[str, Any]) -> str:
            """上流の次のページ（p2）だけ発行を断る。"""
            if position["page"] == "p2":
                raise facade.errors.FacadeError("too long")
            return _readable_cursor(position)

        with pytest.raises(facade.errors.FacadeError, match="too long"):
            self._fit(facade, self._ITEMS, issue=refuse_upstream_page)

    def test_result_always_fits_budget(self, facade: types.SimpleNamespace) -> None:
        """どの予算でも、返した結果全体は予算に収まる（見積もりは実際の cursor と notes 以上）。"""
        longest_note = max(facade.shaping.REDUCED_NOTE, self._EMPTY, key=len)
        smallest = len(facade.shaping.compact_json(_page_result(self._ITEMS[:1], "cursor:p1:9", [longest_note])))
        for budget in range(smallest, smallest + 400, 7):
            for upstream_next in ("p2", None):
                result = self._fit(facade, self._ITEMS, max_chars=budget, upstream_next=upstream_next)
                assert len(facade.shaping.compact_json(result)) <= budget, (budget, upstream_next)
