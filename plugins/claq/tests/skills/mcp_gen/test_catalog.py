"""facade/catalog.py（項目カタログと、検索結果の 1 件に出す項目）のテスト。"""

from __future__ import annotations

import types
from typing import Any

import pytest


def _catalog(facade: types.SimpleNamespace) -> Any:
    """見本の項目カタログを作る。"""
    spec = facade.catalog.FieldSpec
    return facade.catalog.Catalog(
        "order",
        [
            spec("order_id", "string", "受注 ID", "public", views=("summary", "detail")),
            spec("shipping.address.city", "string", "配送先の市区町村", "internal", views=("detail",), in_search=True),
            spec("shipping.address.postal_code", "string", "配送先の郵便番号", "personal"),
            spec("cost.amount", "number", "原価", "confidential", views=("detail",)),
        ],
    )


class TestCatalog:
    """項目カタログ。"""

    def test_sensitivity_is_required(self, facade: types.SimpleNamespace) -> None:
        """機微区分は省略できず、path・type・description の次に置く（付け忘れを既定値で通さない）。"""
        with pytest.raises(TypeError):
            facade.catalog.FieldSpec("a", "string", "x")
        spec = facade.catalog.FieldSpec("a", "string", "x", "public", ("summary",), True)
        assert (spec.sensitivity, spec.views, spec.in_search) == ("public", ("summary",), True)

    @pytest.mark.parametrize(
        ("specs", "match"),
        [
            ([("a", "string", "x", "internal"), ("a", "string", "y", "internal")], "重複"),
            ([("a", "string", "x", "secret")], "機微区分"),
            ([("a.b", "string", "x", "personal", ("detail",))], "personal"),
            ([("a", "string", "x", "internal"), ("a.b.c", "string", "y", "personal")], "葉"),
            *(([("a", "string", "x", "internal"), ("b", field_type, "y", "internal")], "スカラー") for field_type in ["object", "array", "null", "String", ""]),
            *(([(path, "string", "x", "internal")], "パスの書式") for path in ["Order_id", "a..b", "a.", ".a", "a b", "a-b", "住所", "a" * 101, ""]),
        ],
    )
    def test_rejects_bad_definitions(self, facade: types.SimpleNamespace, specs: list[tuple[Any, ...]], match: str) -> None:
        """定義の誤りを弾く: パスの重複・未知の機微区分・ビューに入れた個人情報（fields で明示されたときだけ返す）・別の項目の祖先のパス（祖先を指定すると子の機微区分を素通りして全体が返る）・JSON のスカラーでない葉の型（object の葉は部分木を丸ごと返す）・ツールの fields 引数の要素と同じ書式（PATH_PATTERN）と長さ（PATH_MAX_CHARS）に収まらないパス（入口で受け付けないパスを載せない）。"""
        with pytest.raises(ValueError, match=match):
            facade.catalog.Catalog("o", [facade.catalog.FieldSpec(*spec) for spec in specs])

    @pytest.mark.parametrize(
        "fields",
        [
            [("a" * 100, "string"), ("shipping.address.postal_code", "string"), ("items2.unit_price.amount", "string")],
            [("string", "string"), ("number", "number"), ("integer", "integer"), ("boolean", "boolean")],
            [("item", "string"), ("items.sku", "string")],
        ],
        ids=["paths-within-input-limits", "scalar-types", "prefix-sibling-is-not-ancestor"],
    )
    def test_accepts_valid_definitions(self, facade: types.SimpleNamespace, fields: list[tuple[str, str]]) -> None:
        """ドット区切りの英小文字・数字・_ で長さが上限（PATH_MAX_CHARS）ちょうどまでのパス、JSON のスカラーの 4 つの型、祖先ではない前方一致の兄弟のパスは、定義順のまま載せる。"""
        catalog = facade.catalog.Catalog("o", [facade.catalog.FieldSpec(path, field_type, "x", "internal") for path, field_type in fields])
        assert [(entry["path"], entry["type"]) for entry in catalog.describe(None, limit=10)["fields"]] == fields

    def test_view(self, facade: types.SimpleNamespace) -> None:
        """ビューに含まれるパスを定義順に返し、主体に返してよい機微区分だけに絞る。"""
        catalog = _catalog(facade)
        everything = set(facade.catalog.SENSITIVITY_LEVELS)
        assert catalog.view("detail", allowed_sensitivity=everything) == ["order_id", "shipping.address.city", "cost.amount"]
        assert catalog.view("detail", allowed_sensitivity={"public"}) == ["order_id"]

    def test_personal_paths(self, facade: types.SimpleNamespace) -> None:
        """与えたパスのうち、個人情報（personal）のものだけを順序を保って返す（監査ログ用）。"""
        catalog = _catalog(facade)
        assert catalog.personal_paths(["order_id", "shipping.address.postal_code"]) == ["shipping.address.postal_code"]
        assert catalog.personal_paths(["order_id", "cost.amount"]) == []

    def test_describe(self, facade: types.SimpleNamespace) -> None:
        """パスか説明の部分一致で返し、上限を超えたら has_more を立てる。"""
        catalog = _catalog(facade)
        described = catalog.describe("配送", limit=1)
        assert described["total_matches"] == 2
        assert described["has_more"] is True
        assert described["fields"] == [
            {
                "path": "shipping.address.city",
                "type": "string",
                "description": "配送先の市区町村",
                "views": ["detail"],
                "sensitivity": "internal",
                "in_search": True,
            }
        ]
        assert catalog.describe("ORDER_ID", limit=10)["total_matches"] == 1
        assert catalog.describe(None, limit=10)["total_matches"] == 4

    def test_resolve(self, facade: types.SimpleNamespace) -> None:
        """未知・検索で使えない・権限の無いパスを、次の行動を添えて拒否する。"""
        catalog = _catalog(facade)
        allowed = {"public", "internal"}
        assert catalog.resolve(["shipping.address.city"], for_search=True, allowed_sensitivity=allowed) == [
            "shipping.address.city"
        ]
        with pytest.raises(facade.errors.FacadeError, match="近いパス: shipping.address.city"):
            catalog.resolve(["shiping.address.city"], for_search=False, allowed_sensitivity=allowed)
        with pytest.raises(facade.errors.FacadeError, match="fields_describe") as unknown:
            catalog.resolve(["zzz"], for_search=False, allowed_sensitivity=allowed)
        assert "近いパス" not in str(unknown.value)
        with pytest.raises(facade.errors.FacadeError, match="検索結果には付けられません"):
            catalog.resolve(["order_id"], for_search=True, allowed_sensitivity=allowed)
        with pytest.raises(facade.errors.FacadeError, match="権限"):
            catalog.resolve(["shipping.address.postal_code"], for_search=False, allowed_sensitivity=allowed)


def _summary_catalog(facade: types.SimpleNamespace) -> Any:
    """summary ビューに区分の違う項目を持つカタログを作る（検索結果の要約の検査用）。"""
    spec = facade.catalog.FieldSpec
    return facade.catalog.Catalog(
        "order",
        [
            spec("order_id", "string", "受注 ID", "internal", views=("summary", "detail")),
            spec("label", "string", "注文番号", "internal", views=("summary",)),
            spec("total.amount", "number", "合計金額", "confidential", views=("summary",)),
            spec("customer.label", "string", "顧客名", "internal", views=("detail",)),
            spec("shipping.address.postal_code", "string", "郵便番号", "personal"),
        ],
    )


class TestSummaryView:
    """検索結果の 1 件に出す項目（summary ビューを主体に許された機微区分で絞る）。"""

    @pytest.mark.parametrize(
        ("allowed", "expected"),
        [({"internal", "confidential"}, ["order_id", "label", "total.amount"]), ({"internal"}, ["order_id", "label"])],
        ids=["all-allowed", "confidential-not-allowed"],
    )
    def test_summary_view(self, facade: types.SimpleNamespace, allowed: set[str], expected: list[str]) -> None:
        """summary ビューの項目を定義順に返し、主体に許されない区分（confidential）の項目は要約にも出さない。"""
        assert _summary_catalog(facade).summary_view("order_id", allowed_sensitivity=allowed) == expected

    def test_summary_view_id_not_allowed_raises_facade_error(self, facade: types.SimpleNamespace) -> None:
        """識別の項目（order_id）自体が許されない区分なら、識別できない候補を返さずに FacadeError にする。"""
        catalog = _summary_catalog(facade)
        with pytest.raises(facade.errors.FacadeError, match="order_id"):
            catalog.summary_view("order_id", allowed_sensitivity={"public", "confidential"})

    @pytest.mark.parametrize("id_path", ["customer.label", "missing"])
    def test_summary_view_id_outside_view_raises_value_error(self, facade: types.SimpleNamespace, id_path: str) -> None:
        """識別の項目が summary ビューに無いのはカタログの定義の誤りなので、ValueError にする。"""
        catalog = _summary_catalog(facade)
        with pytest.raises(ValueError, match="summary"):
            catalog.summary_view(id_path, allowed_sensitivity={"internal", "confidential"})
