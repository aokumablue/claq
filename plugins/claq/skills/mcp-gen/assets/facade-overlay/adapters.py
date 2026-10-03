"""上流 API のアダプタ（見本: 受注 API と配送 API の偽物。実システムでは置き換える）。

偽物は、上流によくある形（包み・略語・数値コード・UTC の日時・内部項目）の応答を
返す。正規ドメインモデルへの変換（`normalize_order`）までがアダプタの責務である。

実際の HTTP 呼び出しに置き換えても守ること:
- 上流の失敗は `UpstreamError(status, detail, retry_after=...)` で投げる
- 上流のページ位置（ページトークン）は不透明なまま返す（カーソルへの封入は server.py）
- 上流の応答は `normalize_*` で正規ドメインモデルにしてから返す
- タイムアウトは呼び出しごとに設定する（全体の締切は server.py が持つ）
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from facade.shaping import clean_text
from facade.upstream import UpstreamError

JST = timezone(timedelta(hours=9), "JST")
NOTE_MAX_CHARS = 500

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
                "memo": "至急\x1b[31m 対応​お願いします" if number == 7 else "",
                "_links": {"self": f"/orders/ord_{number:06d}"},
                "internalFlags": {"legacy": number % 2 == 0},
            },
        }
    }


_ORDERS = [_raw_order(number) for number in range(1, 26)]


def _created_at(raw: dict[str, Any]) -> datetime:
    """上流の受注の作成日時を、タイムゾーン付きで返す。

    Args:
        raw: 上流の形の受注。

    Returns:
        作成日時。
    """
    return datetime.fromisoformat(raw["data"]["attributes"]["createdAt"].replace("Z", "+00:00"))


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
            上流の形の配送の一覧。

        Raises:
            UpstreamError: 部分失敗の見本の受注では 503。
        """
        if order_id == UNAVAILABLE_SHIPMENTS_ORDER:
            raise UpstreamError(503, "shipments api maintenance", retry_after=0.0)
        number = int(order_id.removeprefix("ord_"))
        return [{"trackingNo": f"TRK{number:08d}{index}", "st": "PTD"[index % 3]} for index in range(number % 3)]


def normalize_order(raw: dict[str, Any], shipments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """上流の受注を正規ドメインモデルにする。

    包みを外し、略語を意味の分かる名前にし、数値コードを文字列にし、日時を JST に揃え、
    金額を amount（通貨の標準単位の数値）と currency の組にし、内部項目（`_links` / `internalFlags`）を落とす。
    自由記述は無害化して専用の項目に入れる。

    Args:
        raw: 上流の形の受注。
        shipments: 上流の形の配送。取得していなければ None（項目ごと省く）。

    Returns:
        正規ドメインモデルの受注。
    """
    attributes = raw["data"]["attributes"]
    ordered_at = _created_at(raw).astimezone(JST)
    customer_id = attributes["customerRef"]
    order: dict[str, Any] = {
        "order_id": raw["data"]["id"],
        "label": f"{attributes['number']}（{_CUSTOMERS[customer_id]}、{ordered_at.date().isoformat()}）",
        "status": _STATUS_BY_CODE[attributes["ordSts"]],
        "ordered_at": ordered_at.isoformat(),
        "total": {"amount": float(attributes["grandTotal"]["value"]), "currency": attributes["grandTotal"]["ccy"]},
        "customer": {"customer_id": customer_id, "label": _CUSTOMERS[customer_id]},
        "shipping": {
            "address": {"city": attributes["shipTo"]["addr"]["city"], "postal_code": attributes["shipTo"]["addr"]["zip"]}
        },
        "items": [
            {
                "sku": line["sku"],
                "quantity": line["qty"],
                "unit_price": {"amount": float(line["unitPrice"]["value"]), "currency": line["unitPrice"]["ccy"]},
            }
            for line in attributes["lines"]
        ],
        "note": clean_text(attributes["memo"], max_chars=NOTE_MAX_CHARS) or None,
    }
    if shipments is not None:
        order["shipments"] = [
            {"tracking_number": shipment["trackingNo"], "state": _SHIPMENT_STATES[shipment["st"]]} for shipment in shipments
        ]
    return order
