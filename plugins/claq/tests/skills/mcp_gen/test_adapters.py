"""重ね合わせの adapters.py（見本の上流の偽物と、正規ドメインモデルへの正規化）のテスト。"""

from __future__ import annotations

import asyncio
import copy
import sys
import types
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from ._support import _FACADE_MODULES, _OVERLAY, _is_invisible, _load


@pytest.fixture
def adapters(facade: types.SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """重ね合わせの adapters.py を読み込む。

    adapters.py は部品を `facade.*` の名前で import する。読み込み済みの部品を、同じ
    モジュールオブジェクトのままその名前でも登録する（UpstreamError の同一性を保つ）。
    登録はテストの後で戻す。
    """
    monkeypatch.setitem(sys.modules, "facade", sys.modules["mcp_gen_facade"])
    for name in _FACADE_MODULES:
        monkeypatch.setitem(sys.modules, f"facade.{name}", getattr(facade, name))
    return _load("mcp_gen_adapters", _OVERLAY / "adapters.py")


def _strings(value: Any) -> Iterator[str]:
    """値の中の文字列を、入れ子をたどって全て列挙する。

    Args:
        value: JSON の形の値。

    Yields:
        値の中の文字列。
    """
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
        return
    if isinstance(value, list):
        for child in value:
            yield from _strings(child)


_DELETE = object()


def _replaced(document: Any, path: str, value: Any) -> Any:
    """document を深く複製し、ドット区切りのパスの値を置き換えて返す（不正な上流の応答を作る）。

    Args:
        document: 元の値（変更しない）。
        path: ドット区切りのパス。数字の部分は配列の添字。
        value: 置き換える値。`_DELETE` なら項目を消す。

    Returns:
        置き換えた複製。
    """
    copied = copy.deepcopy(document)
    *parents, last = path.split(".")
    node = copied
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    key: Any = int(last) if isinstance(node, list) else last
    if value is _DELETE:
        del node[key]
    else:
        node[key] = value
    return copied


def _normalize_replaced(adapters: types.ModuleType, path: str, value: Any) -> dict[str, Any]:
    """見本の受注 1 と配送 1 件の、パスの値を置き換えて正規化する。

    Args:
        adapters: 読み込んだ adapters モジュール。
        path: `order.` か `shipments.` で始まるパス。
        value: 置き換える値。

    Returns:
        正規化した受注。
    """
    upstream = _replaced({"order": adapters._raw_order(1), "shipments": [{"trackingNo": "TRK1", "st": "D"}]}, path, value)
    return {**adapters.normalize_order(upstream["order"]), "shipments": adapters.normalize_shipments(upstream["shipments"])}


def _search(adapters: types.ModuleType, **conditions: Any) -> tuple[list[str], str | None]:
    """偽の受注 API を検索し、受注 ID の一覧と次のページトークンを返す。

    Args:
        adapters: 読み込んだ adapters モジュール。
        **conditions: 既定の条件に上書きする検索条件。

    Returns:
        （受注 ID の一覧、次のページトークン）。
    """
    arguments = {
        "text": None,
        "customer_ref": None,
        "status_codes": None,
        "created_from": None,
        "created_before": None,
        "sort": "ordered_at_asc",
        "page": None,
        "page_size": 50,
        **conditions,
    }
    raw_page, next_page = asyncio.run(adapters.OrdersApi().search(**arguments))
    return [raw["data"]["id"] for raw in raw_page], next_page


class TestAdapters:
    """重ね合わせの見本の上流（偽物）と正規化。"""

    def test_normalize_order(self, adapters: types.ModuleType) -> None:
        """包み・略語・数値コード・UTC を外して正規ドメインモデルにし、備考は無害化する。"""
        order = adapters.normalize_order(adapters._raw_order(7))
        assert order == {
            "order_id": "ord_000007",
            "label": "ORD-2026-0007（2026-09-05）",
            "status": "shipped",
            "ordered_at": "2026-09-05T05:00:00+09:00",
            "total": {"amount": 10900.0, "currency": "JPY"},
            "customer": {"customer_id": "cus_0000000002", "label": "佐藤工業"},
            "shipping": {"address": {"city": "東京都千代田区", "postal_code": "5300007"}},
            "items": [
                {"sku": f"SKU-007-0{line}", "quantity": line % 3 + 1, "unit_price": {"amount": 1000.0 + 100 * line, "currency": "JPY"}}
                for line in range(5)
            ],
            "note": "至急 対応お願いします",
        }

    def test_normalize_shipments(self, adapters: types.ModuleType) -> None:
        """配送は受注とは別に正規化する（形の不正を配送だけの部分失敗にできるように）。"""
        assert adapters.normalize_shipments([{"trackingNo": "TRK1", "st": "T"}]) == [
            {"tracking_number": "TRK1", "state": "in_transit"}
        ]
        assert adapters.normalize_shipments([]) == []
        with pytest.raises(adapters.UpstreamError) as raised:
            adapters.normalize_shipments([{"trackingNo": "TRK1", "st": "X"}])
        assert raised.value.status == 502

    def test_normalize_omits_shipments_and_empty_note(self, adapters: types.ModuleType) -> None:
        """受注の正規化は配送を含めず（normalize_shipments で別に作る）、空の備考は null にする。"""
        order = adapters.normalize_order(adapters._raw_order(1))
        assert "shipments" not in order
        assert order["note"] is None

    def test_label_has_no_customer_name(self, adapters: types.ModuleType) -> None:
        """label は注文番号（注文日）だけにする（顧客名は customer.label の項目で取らせる）。"""
        for raw in adapters._ORDERS:
            order = adapters.normalize_order(raw)
            assert order["customer"]["label"] not in order["label"], order["label"]

    def test_normalize_cleans_every_upstream_string(self, adapters: types.ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
        """上流由来の全ての文字列から制御列・不可視文字を除き、長さを定数の上限で抑える。"""
        dirt = "\x1b[31m\x07\u202e" + "x" * 2000
        raw = adapters._raw_order(1)
        attributes = raw["data"]["attributes"]
        raw["data"]["id"] += dirt
        attributes["number"] += dirt
        monkeypatch.setitem(adapters._CUSTOMERS, attributes["customerRef"] + dirt, "田中商事" + dirt)
        attributes["customerRef"] += dirt
        attributes["grandTotal"]["ccy"] += dirt
        attributes["shipTo"]["addr"]["city"] += dirt
        attributes["shipTo"]["addr"]["zip"] += dirt
        for line in attributes["lines"]:
            line["sku"] += dirt
            line["unitPrice"]["ccy"] += dirt
        attributes["memo"] = "備考" + dirt
        order = {
            **adapters.normalize_order(raw),
            "shipments": adapters.normalize_shipments([{"trackingNo": "TRK1" + dirt, "st": "D"}]),
        }
        strings = list(_strings(order))
        assert len(strings) == 12 + 2 * len(attributes["lines"])
        for text in strings:
            assert not any(_is_invisible(char) for char in text), text
            assert "[31m" not in text, text
            assert len(text) <= adapters.NOTE_MAX_CHARS + 1, text
        assert order["customer"]["label"].startswith("田中商事x")
        assert order["note"] == "備考" + "x" * (adapters.NOTE_MAX_CHARS - 2) + "…"
        assert len(order["order_id"]) == adapters.ID_MAX_CHARS + 1

    @pytest.mark.parametrize("quantity", ["2", {"n": 2}, True, False, 2.5, 2.0, None])
    def test_normalize_quantity_not_int_raises_502(
        self, adapters: types.ModuleType, facade: types.SimpleNamespace, quantity: Any
    ) -> None:
        """明細の数量は int（bool を除く）だけを通し、文字列・object・bool・小数は上流の失敗（502）にする。"""
        with pytest.raises(facade.upstream.UpstreamError) as malformed:
            _normalize_replaced(adapters, "order.data.attributes.lines.0.qty", quantity)
        assert malformed.value.status == 502

    @pytest.mark.parametrize(
        ("path", "value"),
        [
            ("order", []),
            ("order.data", _DELETE),
            ("order.data", "x"),
            ("order.data.id", 1),
            ("order.data.attributes", _DELETE),
            ("order.data.attributes.number", 7),
            ("order.data.attributes.ordSts", 5),
            ("order.data.attributes.ordSts", True),
            ("order.data.attributes.ordSts", 1.0),
            ("order.data.attributes.ordSts", "1"),
            ("order.data.attributes.createdAt", 0),
            ("order.data.attributes.createdAt", "yesterday"),
            ("order.data.attributes.createdAt", "2026-09-01T01:00:00"),
            ("order.data.attributes.createdAt", "9999-12-31T23:00:00Z"),
            ("order.data.attributes.customerRef", 1),
            ("order.data.attributes.customerRef", "cus_9999999999"),
            ("order.data.attributes.grandTotal", 100),
            ("order.data.attributes.grandTotal.value", True),
            ("order.data.attributes.grandTotal.value", "100"),
            ("order.data.attributes.grandTotal.value", float("nan")),
            ("order.data.attributes.grandTotal.value", float("inf")),
            pytest.param("order.data.attributes.grandTotal.value", 10**400, id="grandTotal.value-overflow"),
            ("order.data.attributes.grandTotal.ccy", 392),
            ("order.data.attributes.shipTo.addr", "大阪市"),
            ("order.data.attributes.shipTo.addr.city", None),
            ("order.data.attributes.shipTo.addr.zip", 5300001),
            ("order.data.attributes.lines", {"sku": "SKU"}),
            ("order.data.attributes.lines.0", "SKU"),
            ("order.data.attributes.lines.0.sku", None),
            ("order.data.attributes.lines.0.unitPrice", _DELETE),
            ("order.data.attributes.lines.0.unitPrice.value", "1000"),
            ("order.data.attributes.memo", None),
            ("shipments", {"trackingNo": "TRK1"}),
            ("shipments.0", "TRK1"),
            ("shipments.0.trackingNo", 1),
            ("shipments.0.st", "X"),
            ("shipments.0.st", 1),
        ],
    )
    def test_normalize_malformed_upstream_raises_502(
        self, adapters: types.ModuleType, facade: types.SimpleNamespace, path: str, value: Any
    ) -> None:
        """上流の値の型・コード・日時・数値・入れ物が想定と違えば、素通しも汎用の例外もせず、上流の失敗（502）にする。"""
        with pytest.raises(facade.upstream.UpstreamError) as malformed:
            _normalize_replaced(adapters, path, value)
        assert malformed.value.status == 502

    def test_normalize_malformed_detail_names_field_only(self, adapters: types.ModuleType, facade: types.SimpleNamespace) -> None:
        """502 の detail（運用者のログに出る）は項目の名前だけを書き、上流の値は書かない。"""
        with pytest.raises(facade.upstream.UpstreamError) as malformed:
            _normalize_replaced(adapters, "order.data.attributes.lines.0.qty", "SECRET-42")
        assert ("qty" in malformed.value.detail, "SECRET-42" in str(malformed.value)) == (True, False)

    def test_search_pages_with_upstream_tokens(self, adapters: types.ModuleType) -> None:
        """ページトークンを受け渡して続きをたどり、最後のページでは None を返す。"""
        expected = [raw["data"]["id"] for raw in adapters._ORDERS if raw["data"]["attributes"]["customerRef"] == "cus_0000000001"]
        seen: list[str] = []
        tokens: list[str | None] = []
        page = None
        while True:
            ids, page = _search(adapters, customer_ref="cus_0000000001", page=page, page_size=3)
            seen += ids
            tokens.append(page)
            if page is None:
                break
        assert seen == expected
        assert tokens == ["o:3", "o:6", None]

    def test_search_rejects_broken_token(self, adapters: types.ModuleType, facade: types.SimpleNamespace) -> None:
        """壊れたページトークンは 400 の UpstreamError（部品と同じクラス）にする。"""
        with pytest.raises(facade.upstream.UpstreamError) as broken:
            _search(adapters, page="o:x")
        assert broken.value.status == 400

    def test_search_filters_and_sorts(self, adapters: types.ModuleType) -> None:
        """注文番号・顧客名の部分一致、状態、作成日時の範囲で絞り、合計の降順にも並べる。"""
        assert _search(adapters, text="ORD-2026-0007")[0] == ["ord_000007"]
        assert len(_search(adapters, text="佐藤")[0]) == len(_search(adapters, customer_ref="cus_0000000002")[0])
        assert _search(adapters, status_codes=[9])[0] == [f"ord_{number:06d}" for number in range(1, 26) if number % 5 == 4]
        start = adapters._created_at(adapters._ORDERS[1])
        assert _search(adapters, created_from=start, created_before=start + timedelta(hours=26))[0] == ["ord_000002", "ord_000003"]
        totals = [adapters._raw_order(int(order_id[4:]))["data"]["attributes"]["grandTotal"]["value"] for order_id in _search(adapters, sort="total_desc")[0]]
        assert totals == sorted(totals, reverse=True)
        newest_first = _search(adapters, sort="ordered_at_desc")[0]
        assert newest_first[0] == "ord_000025"

    def test_get(self, adapters: types.ModuleType, facade: types.SimpleNamespace) -> None:
        """受注を 1 件返し、無ければ 404 の UpstreamError にする。"""
        assert asyncio.run(adapters.OrdersApi().get("ord_000002"))["data"]["id"] == "ord_000002"
        with pytest.raises(facade.upstream.UpstreamError) as missing:
            asyncio.run(adapters.OrdersApi().get("ord_999999"))
        assert missing.value.status == 404

    def test_shipments(self, adapters: types.ModuleType, facade: types.SimpleNamespace) -> None:
        """配送を返し、部分失敗の見本は 503、数値でない ID は例外を投げず空にする。"""
        shipments = adapters.ShipmentsApi()
        assert asyncio.run(shipments.list_for_order("ord_000004")) == [{"trackingNo": "TRK000000040", "st": "P"}]
        with pytest.raises(facade.upstream.UpstreamError) as unavailable:
            asyncio.run(shipments.list_for_order(adapters.UNAVAILABLE_SHIPMENTS_ORDER))
        assert unavailable.value.status == 503
        for order_id in ("ord_abc", "ord_", "ord_１２３", "ord_1_000"):
            assert asyncio.run(shipments.list_for_order(order_id)) == []
