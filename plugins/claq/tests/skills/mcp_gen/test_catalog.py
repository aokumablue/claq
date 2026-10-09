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

    def test_rejects_bad_definitions(self, facade: types.SimpleNamespace) -> None:
        """パスの重複と未知の機微区分は定義の誤りとして弾く。"""
        spec = facade.catalog.FieldSpec
        with pytest.raises(ValueError, match="重複"):
            facade.catalog.Catalog("o", [spec("a", "string", "x", "internal"), spec("a", "string", "y", "internal")])
        with pytest.raises(ValueError, match="機微区分"):
            facade.catalog.Catalog("o", [spec("a", "string", "x", "secret")])

    @pytest.mark.parametrize("path", ["Order_id", "a..b", "a.", ".a", "a b", "a-b", "住所", "a" * 101, ""])
    def test_rejects_paths_outside_input_limits(self, facade: types.SimpleNamespace, path: str) -> None:
        """パスはツールの fields 引数の要素と同じ書式（PATH_PATTERN）と長さ（PATH_MAX_CHARS）に収める（入口で受け付けないパスを載せない）。"""
        with pytest.raises(ValueError, match="パスの書式"):
            facade.catalog.Catalog("o", [facade.catalog.FieldSpec(path, "string", "x", "internal")])

    def test_accepts_paths_within_input_limits(self, facade: types.SimpleNamespace) -> None:
        """ドット区切りの英小文字・数字・_ で、長さが上限ちょうどまでのパスは通す。"""
        spec = facade.catalog.FieldSpec
        paths = ["a" * facade.catalog.PATH_MAX_CHARS, "shipping.address.postal_code", "items2.unit_price.amount"]
        catalog = facade.catalog.Catalog("o", [spec(path, "string", "x", "internal") for path in paths])
        assert [entry["path"] for entry in catalog.describe(None, limit=10)["fields"]] == paths

    def test_rejects_personal_in_views(self, facade: types.SimpleNamespace) -> None:
        """個人情報（personal）の項目はビューに入れない（fields で明示されたときだけ返す）。"""
        spec = facade.catalog.FieldSpec
        with pytest.raises(ValueError, match="personal"):
            facade.catalog.Catalog("o", [spec("a.b", "string", "x", "personal", views=("detail",))])

    @pytest.mark.parametrize("field_type", ["object", "array", "null", "String", ""])
    def test_catalog_non_scalar_type_raises(self, facade: types.SimpleNamespace, field_type: str) -> None:
        """葉の型は JSON のスカラーに限り、object などは定義の誤りにする（object の葉は部分木を丸ごと返す）。"""
        spec = facade.catalog.FieldSpec
        with pytest.raises(ValueError, match="スカラー"):
            facade.catalog.Catalog("o", [spec("a", "string", "x", "internal"), spec("b", field_type, "y", "internal")])

    def test_catalog_scalar_types_accepted(self, facade: types.SimpleNamespace) -> None:
        """JSON のスカラーの 4 つの型（string / number / integer / boolean）は受け付ける。"""
        spec = facade.catalog.FieldSpec
        scalars = ["string", "number", "integer", "boolean"]
        catalog = facade.catalog.Catalog("o", [spec(name, name, "x", "internal") for name in scalars])
        assert [field["type"] for field in catalog.describe(None, limit=10)["fields"]] == scalars

    def test_rejects_ancestor_paths(self, facade: types.SimpleNamespace) -> None:
        """別の項目の祖先のパスは載せない（祖先を指定すると、子の機微区分を素通りして全体が返る）。"""
        spec = facade.catalog.FieldSpec
        with pytest.raises(ValueError, match="葉"):
            facade.catalog.Catalog("o", [spec("a", "string", "x", "internal"), spec("a.b.c", "string", "y", "personal")])
        siblings = facade.catalog.Catalog("o", [spec("item", "string", "x", "internal"), spec("items.sku", "string", "y", "internal")])
        assert siblings.describe(None, limit=10)["total_matches"] == 2

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

    def test_summary_view_all_allowed(self, facade: types.SimpleNamespace) -> None:
        """全ての区分が許されていれば、summary ビューの項目を定義順に返す。"""
        catalog = _summary_catalog(facade)
        assert catalog.summary_view("order_id", allowed_sensitivity={"internal", "confidential"}) == ["order_id", "label", "total.amount"]

    def test_summary_view_drops_disallowed_sensitivity(self, facade: types.SimpleNamespace) -> None:
        """主体に許されない区分（confidential）の項目は、要約にも出さない。"""
        catalog = _summary_catalog(facade)
        assert catalog.summary_view("order_id", allowed_sensitivity={"internal"}) == ["order_id", "label"]

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
