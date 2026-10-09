"""上流 API のアダプタ（見本: 受注 API と配送 API の偽物。実システムでは置き換える）。

偽物は、上流によくある形（包み・略語・数値コード・UTC の日時・内部項目）の応答を
返す。正規ドメインモデルへの変換（`normalize_order`・`normalize_shipments`）までがアダプタの責務である。
上流ごとに別の関数で変換する（ある上流の応答の形の不正を、その上流だけの部分失敗にできるように）。

実際の HTTP 呼び出しに置き換えても守ること:
- 上流の失敗は `UpstreamError(status, detail, retry_after=...)` で投げる
- 上流のページ位置（ページトークン）は不透明なまま返す（カーソルへの封入は server.py）
- 上流の応答は `normalize_*` で正規ドメインモデルにしてから返す。正規ドメインモデルは
  dict・list・JSON のスカラーだけで組む（tuple・dataclass・pydantic のモデルを入れない。
  facade の射影はそれ以外の値に当たると失敗する）
- 上流から来た値は全て型を確かめてから出す（数値・コード・日時・入れ物も）。想定と違えば
  素通しも汎用の例外もせず、上流の失敗 `UpstreamError(502, 項目の名前)` にする
- 上流から来た文字列は全て `clean_text` に長さの上限を付けて通す（ID・コード・名前も）
- タイムアウトは呼び出しごとに設定する（全体の締切は server.py が持つ）
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from facade.shaping import clean_text
from facade.upstream import UpstreamError

JST = timezone(timedelta(hours=9), "JST")
# 上流から来た文字列の長さの上限（clean_text の max_chars）
ID_MAX_CHARS = 64  # 受注 ID・顧客 ID
CODE_MAX_CHARS = 32  # 注文番号・郵便番号・SKU・通貨・追跡番号
NAME_MAX_CHARS = 100  # 顧客名・市区町村
NOTE_MAX_CHARS = 500  # 備考（自由記述）

_STATUS_BY_CODE = {1: "open", 2: "paid", 3: "shipped", 4: "delivered", 9: "cancelled"}
STATUS_CODES = {name: code for code, name in _STATUS_BY_CODE.items()}
_CUSTOMERS = {"cus_0000000001": "田中商事", "cus_0000000002": "佐藤工業", "cus_0000000003": "鈴木物産"}
_CITIES = ("大阪市", "東京都千代田区", "名古屋市")
_SHIPMENT_STATES = {"P": "preparing", "T": "in_transit", "D": "delivered"}
# 部分失敗の見本: この受注の配送は配送 API が 503 を返す
UNAVAILABLE_SHIPMENTS_ORDER = "ord_000003"


def _raw_order(number: int) -> dict[str, Any]:
    """上流の形の受注を 1 件作る（見本のデータ）。

    Args:
        number: 受注の連番。

    Returns:
        上流 API が返す形の受注。
    """
    lines = [
        {"sku": f"SKU-{number:03d}-{line:02d}", "qty": line % 3 + 1, "unitPrice": {"value": 1000 + 100 * line, "ccy": "JPY"}}
        for line in range(1 + number * 7 % 15)
    ]
    created = datetime(2026, 9, 1, 1, 0) + timedelta(hours=13 * number)
    return {
        "data": {
            "id": f"ord_{number:06d}",
            "type": "order",
            "attributes": {
                "number": f"ORD-2026-{number:04d}",
                "ordSts": list(_STATUS_BY_CODE)[number % len(_STATUS_BY_CODE)],
                "createdAt": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "customerRef": f"cus_{number % 3 + 1:010d}",
                "grandTotal": {"value": sum(line["qty"] * line["unitPrice"]["value"] for line in lines), "ccy": "JPY"},
                "shipTo": {"addr": {"city": _CITIES[number % 3], "zip": f"{5300000 + number:07d}"}},
                "lines": lines,
                "memo": "至急\x1b[31m 対応\u200bお願いします" if number == 7 else "",
                "_links": {"self": f"/orders/ord_{number:06d}"},
                "internalFlags": {"legacy": number % 2 == 0},
            },
        }
    }


_ORDERS = [_raw_order(number) for number in range(1, 26)]


def _malformed(field: str) -> UpstreamError:
    """上流の応答の項目が想定の形でないことを、上流の失敗（502）として表す。

    detail には項目の名前だけを書く（上流の値は運用者のログにも残さない）。

    Args:
        field: 項目の名前（上流のパス）。

    Returns:
        投げる UpstreamError。
    """
    return UpstreamError(502, f"malformed upstream field: {field}")


def _at(value: Any, path: str) -> Any:
    """上流の object を、ドット区切りのパスでたどって値を返す。

    Args:
        value: 上流の object。
        path: ドット区切りのパス（`data.attributes`）。

    Returns:
        パスの先の値。

    Raises:
        UpstreamError: 途中が object でない、または項目が無いとき（502）。
    """
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            raise _malformed(path)
        value = value[key]
    return value


def _text(container: Any, path: str, *, max_chars: int) -> str:
    """上流の文字列の項目を取り出し、`clean_text` で無害化して長さを抑える。

    Args:
        container: 上流の object。
        path: ドット区切りのパス。
        max_chars: 残す最大文字数。

    Returns:
        無害化した文字列。

    Raises:
        UpstreamError: 項目が無い、または文字列でないとき（502）。
    """
    value = _at(container, path)
    if not isinstance(value, str):
        raise _malformed(path)
    return clean_text(value, max_chars=max_chars)


def _integer(container: Any, path: str) -> int:
    """上流の整数の項目を取り出す（bool・小数・文字列は通さない）。

    Args:
        container: 上流の object。
        path: ドット区切りのパス。

    Returns:
        整数。

    Raises:
        UpstreamError: 項目が無い、または int でないとき（502）。
    """
    value = _at(container, path)
    if type(value) is not int:
        raise _malformed(path)
    return value


def _number(container: Any, path: str) -> float:
    """上流の数値の項目を取り出し、有限の float にする。

    Args:
        container: 上流の object。
        path: ドット区切りのパス。

    Returns:
        有限の float。

    Raises:
        UpstreamError: 項目が無い・int か float でない（bool と文字列を含む）・NaN か無限大・
            float に収まらない整数のとき（502）。
    """
    value = _at(container, path)
    if type(value) not in (int, float):
        raise _malformed(path)
    try:
        number = float(value)
    except OverflowError as error:
        raise _malformed(path) from error
    if not math.isfinite(number):
        raise _malformed(path)
    return number


def _code[K](table: Mapping[K, str], container: Any, path: str, kind: type[K]) -> str:
    """上流のコード値の項目を取り出し、意味のある文字列にする。

    Args:
        table: コード値 → 文字列。
        container: 上流の object。
        path: ドット区切りのパス。
        kind: コード値の型（bool の true を 1 として通さないため、型が一致するものだけを引く）。

    Returns:
        コード値に対応する文字列。

    Raises:
        UpstreamError: 項目が無い・型が違う・未知のコード値のとき（502）。
    """
    value = _at(container, path)
    if type(value) is not kind or value not in table:
        raise _malformed(path)
    return table[value]


def _objects(value: Any, field: str) -> list[dict[str, Any]]:
    """上流の値が object の配列であることを確かめる。

    Args:
        value: 上流の値。
        field: 項目の名前（エラーの detail に使う）。

    Returns:
        object の配列。

    Raises:
        UpstreamError: 配列でない、または object でない要素があるとき（502）。
    """
    if not isinstance(value, list) or not all(isinstance(element, dict) for element in value):
        raise _malformed(field)
    return value


def _created_at(raw: Any) -> datetime:
    """上流の受注の作成日時を、JST の日時で返す。

    Args:
        raw: 上流の形の受注。

    Returns:
        作成日時（JST）。

    Raises:
        UpstreamError: 項目が無い・文字列でない・ISO 8601 として読めない・タイムゾーンが無い・
            JST にすると日付の範囲を外れるとき（502）。
    """
    path = "data.attributes.createdAt"
    value = _at(raw, path)
    if not isinstance(value, str):
        raise _malformed(path)
    try:
        created = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # タイムゾーンの無い日時は、astimezone がホストの地方時とみなして黙ってずれるので通さない
        in_jst = created.astimezone(JST) if created.tzinfo else None
    except (ValueError, OverflowError) as error:
        raise _malformed(path) from error
    if in_jst is None:
        raise _malformed(path)
    return in_jst


class OrdersApi:
    """受注 API の偽物。ページトークンは上流の不透明な文字列として扱う。"""

    async def search(
        self,
        *,
        text: str | None,
        customer_ref: str | None,
        status_codes: list[int] | None,
        created_from: datetime | None,
        created_before: datetime | None,
        sort: str,
        page: str | None,
        page_size: int,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """条件に合う受注の 1 ページと、次のページトークンを返す。

        Args:
            text: 注文番号・顧客名の部分一致。
            customer_ref: 顧客 ID。
            status_codes: 状態の数値コード。
            created_from: 作成日時の下限（この時刻を含む）。
            created_before: 作成日時の上限（この時刻を含まない）。
            sort: 並び順（`ordered_at_desc` / `ordered_at_asc` / `total_desc`）。
            page: 前回の次のページトークン（最初のページなら None）。
            page_size: 1 ページの件数。

        Returns:
            （上流の形の受注の一覧、次のページトークン）。最後のページなら None。

        Raises:
            UpstreamError: ページトークンが壊れているとき（400）。
        """
        matched = [
            raw
            for raw in _ORDERS
            if (customer_ref is None or raw["data"]["attributes"]["customerRef"] == customer_ref)
            and (status_codes is None or raw["data"]["attributes"]["ordSts"] in status_codes)
            and (created_from is None or _created_at(raw) >= created_from)
            and (created_before is None or _created_at(raw) < created_before)
            and (
                text is None
                or text in raw["data"]["attributes"]["number"]
                or text in _CUSTOMERS[raw["data"]["attributes"]["customerRef"]]
            )
        ]
        if sort == "total_desc":
            matched.sort(key=lambda raw: -raw["data"]["attributes"]["grandTotal"]["value"])
        else:
            matched.sort(key=_created_at, reverse=sort == "ordered_at_desc")
        try:
            offset = int((page or "o:0").removeprefix("o:"))
        except ValueError as error:
            raise UpstreamError(400, "bad page token") from error
        page = matched[offset : offset + page_size]
        has_next = offset + page_size < len(matched)
        return page, (f"o:{offset + page_size}" if has_next else None)

    async def get(self, order_id: str) -> dict[str, Any]:
        """受注を 1 件返す。

        Args:
            order_id: 受注 ID。

        Returns:
            上流の形の受注。

        Raises:
            UpstreamError: 見つからないとき（404）。
        """
        for raw in _ORDERS:
            if raw["data"]["id"] == order_id:
                return raw
        raise UpstreamError(404, f"order {order_id} not found")


class ShipmentsApi:
    """配送 API の偽物。"""

    async def list_for_order(self, order_id: str) -> list[dict[str, Any]]:
        """受注に紐づく配送を返す。

        Args:
            order_id: 受注 ID。

        Returns:
            上流の形の配送の一覧。数値でない受注 ID は該当なしとして空。

        Raises:
            UpstreamError: 部分失敗の見本の受注では 503。
        """
        if order_id == UNAVAILABLE_SHIPMENTS_ORDER:
            raise UpstreamError(503, "shipments api maintenance", retry_after=0.0)
        digits = order_id.removeprefix("ord_")
        if not (digits.isascii() and digits.isdigit()):
            return []
        number = int(digits)
        return [{"trackingNo": f"TRK{number:08d}{index}", "st": "PTD"[index % 3]} for index in range(number % 3)]


def _money(container: Any, path: str) -> dict[str, Any]:
    """上流の金額（value と ccy）を、amount と currency の組にする。

    Args:
        container: 上流の object。
        path: 金額の項目のパス。

    Returns:
        amount（通貨の標準単位の有限の数値）と currency の組。

    Raises:
        UpstreamError: 金額が object でない・value が有限の数値でない・ccy が文字列でないとき（502）。
    """
    return {
        "amount": _number(container, f"{path}.value"),
        "currency": _text(container, f"{path}.ccy", max_chars=CODE_MAX_CHARS),
    }


def normalize_order(raw: Any) -> dict[str, Any]:
    """上流の受注を正規ドメインモデルにする。

    包みを外し、略語を意味の分かる名前にし、数値コードを文字列にし、日時を JST に揃え、
    金額を amount（通貨の標準単位の数値）と currency の組にし、内部項目（`_links` / `internalFlags`）を落とす。
    上流から来た値は全て型を確かめ（数量は int、金額は有限の数値、コード値は既知のもの、
    日時はタイムゾーン付き）、文字列は `clean_text` で無害化して長さを抑え、自由記述は専用の項目に入れる。
    label は注文番号（注文日）で、顧客名を含めない（顧客名は customer.label の項目で取らせる）。
    配送（shipments）は含めない（別の上流なので `normalize_shipments` で変換する）。

    Args:
        raw: 上流の形の受注。

    Returns:
        正規ドメインモデルの受注。

    Raises:
        UpstreamError: 上流の値が想定の形でないとき（502。モデルには一時的に取得できない旨を返す）。
    """
    attributes = _at(raw, "data.attributes")
    ordered_at = _created_at(raw)
    customer_ref = _at(attributes, "customerRef")
    if not isinstance(customer_ref, str) or customer_ref not in _CUSTOMERS:
        raise _malformed("customerRef")
    number = _text(attributes, "number", max_chars=CODE_MAX_CHARS)
    return {
        "order_id": _text(raw, "data.id", max_chars=ID_MAX_CHARS),
        "label": f"{number}（{ordered_at.date().isoformat()}）",
        "status": _code(_STATUS_BY_CODE, attributes, "ordSts", int),
        "ordered_at": ordered_at.isoformat(),
        "total": _money(attributes, "grandTotal"),
        "customer": {
            "customer_id": clean_text(customer_ref, max_chars=ID_MAX_CHARS),
            "label": clean_text(_CUSTOMERS[customer_ref], max_chars=NAME_MAX_CHARS),
        },
        "shipping": {
            "address": {
                "city": _text(attributes, "shipTo.addr.city", max_chars=NAME_MAX_CHARS),
                "postal_code": _text(attributes, "shipTo.addr.zip", max_chars=CODE_MAX_CHARS),
            }
        },
        "items": [
            {
                "sku": _text(line, "sku", max_chars=CODE_MAX_CHARS),
                "quantity": _integer(line, "qty"),
                "unit_price": _money(line, "unitPrice"),
            }
            for line in _objects(_at(attributes, "lines"), "lines")
        ],
        "note": _text(attributes, "memo", max_chars=NOTE_MAX_CHARS) or None,
    }


def normalize_shipments(raw: Any) -> list[dict[str, Any]]:
    """上流の配送の一覧を正規ドメインモデル（受注の `shipments`）にする。

    Args:
        raw: 上流の形の配送の一覧。

    Returns:
        正規ドメインモデルの配送の一覧。

    Raises:
        UpstreamError: 上流の値が想定の形でないとき（502）。
    """
    return [
        {
            "tracking_number": _text(shipment, "trackingNo", max_chars=CODE_MAX_CHARS),
            "state": _code(_SHIPMENT_STATES, shipment, "st", str),
        }
        for shipment in _objects(raw, "shipments")
    ]
