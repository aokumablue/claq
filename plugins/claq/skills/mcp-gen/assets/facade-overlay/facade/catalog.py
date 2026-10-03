"""項目カタログ: 項目のパス・型・意味・機微区分を持ち、fields_describe と項目パスの検証に使う。

数百ある上流の項目をツール説明や outputSchema に並べる代わりに、モデルが必要なときに
`fields_describe` で引けるようにする。パスは正規ドメインモデル上のもので、上流の
項目名は使わない。
"""

from __future__ import annotations

import difflib
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .errors import FacadeError

SENSITIVITY_LEVELS = ("public", "internal", "confidential", "personal")


@dataclass(frozen=True)
class FieldSpec:
    """1 項目の定義。

    Attributes:
        path: 正規ドメインモデル上のパス（`shipping.address.city`）。
        type: JSON の型名（string / number / integer / boolean / object）。
        description: 意味・単位・書式。モデルが読む。
        views: この項目を含むビュー（`summary` / `detail`）。
        sensitivity: 機微区分（`SENSITIVITY_LEVELS` のいずれか）。
        in_search: `*_search` の `fields` で指定できるか。
    """

    path: str
    type: str
    description: str
    views: tuple[str, ...] = ()
    sensitivity: str = "internal"
    in_search: bool = False


class Catalog:
    """1 エンティティの項目カタログ。"""

    def __init__(self, entity: str, fields: Sequence[FieldSpec]) -> None:
        """項目の定義を登録する。

        Args:
            entity: エンティティ名（`fields_describe` の `entity` の値）。
            fields: 項目の定義。並び順がそのまま説明とビューの順になる。

        Raises:
            ValueError: パスが重複している、または機微区分が未知のとき。
        """
        self.entity = entity
        self._fields = list(fields)
        self._index = {spec.path: spec for spec in self._fields}
        if len(self._index) != len(self._fields):
            raise ValueError(f"{entity}: 項目のパスが重複している")
        unknown = sorted({spec.sensitivity for spec in self._fields} - set(SENSITIVITY_LEVELS))
        if unknown:
            raise ValueError(f"{entity}: 未知の機微区分 {unknown}")

    def view(self, name: str) -> list[str]:
        """ビューに含まれる項目のパスを返す。

        Args:
            name: ビュー名。

        Returns:
            パスの一覧。
        """
        return [spec.path for spec in self._fields if name in spec.views]

    def describe(self, query: str | None, *, limit: int) -> dict[str, Any]:
        """パスか説明に `query` を含む項目を返す（`fields_describe` の本体）。

        Args:
            query: 部分一致させる語（大文字小文字は区別しない）。None なら全件。
            limit: 返す最大件数。

        Returns:
            エンティティ名・項目の一覧・一致した総数・続きの有無。
        """
        needle = (query or "").casefold()
        matched = [
            spec for spec in self._fields if needle in spec.path.casefold() or needle in spec.description.casefold()
        ]
        return {
            "entity": self.entity,
            "fields": [{**asdict(spec), "views": list(spec.views)} for spec in matched[:limit]],
            "total_matches": len(matched),
            "has_more": len(matched) > limit,
        }

    def resolve(self, paths: Sequence[str], *, for_search: bool, allowed_sensitivity: Collection[str]) -> list[str]:
        """指定された項目のパスを検証し、そのまま返す。

        Args:
            paths: モデルが指定したパス。
            for_search: 検索結果の各候補に付けるための指定か。
            allowed_sensitivity: 呼び出しの主体に返してよい機微区分。

        Returns:
            検証済みのパス。

        Raises:
            FacadeError: 未知のパス・検索結果に付けられないパス・権限の無いパスがあるとき。
        """
        for path in paths:
            spec = self._index.get(path)
            if spec is None:
                close = difflib.get_close_matches(path, list(self._index), n=3)
                hint = f"近いパス: {'、'.join(close)}。" if close else ""
                raise FacadeError(
                    f"未知の項目パス {path!r} があります。{hint}"
                    f"使えるパスは fields_describe（entity: {self.entity}）で調べられます。"
                )
            if for_search and not spec.in_search:
                raise FacadeError(f"{path!r} は検索結果には付けられません。1 件ずつ取得するツールで指定してください。")
            if spec.sensitivity not in allowed_sensitivity:
                raise FacadeError(f"{path!r} は閲覧の権限が無いため返せません。")
        return list(paths)
