"""項目カタログ: 項目のパス・型・意味・機微区分を持ち、fields_describe と項目パスの検証に使う。

数百ある上流の項目をツール説明や outputSchema に並べる代わりに、モデルが必要なときに
`fields_describe` で引けるようにする。パスは正規ドメインモデル上のもので、上流の
項目名は使わない。
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Collection, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from .errors import FacadeError

SENSITIVITY_LEVELS = ("public", "internal", "confidential", "personal")
# 葉の型（JSON のスカラー）。object の葉を許すと、その下の部分木（区分の違う子を含む）が丸ごと返る。
FIELD_TYPES = ("string", "number", "integer", "boolean")
# 項目のパスの書式と長さの上限。ツールの fields 引数の要素にも同じ定数で上限を付け、
# 入口で受け付けないパスをカタログに載せない（長い文字列を候補探しやエラー文に通さない）。
PATH_PATTERN = r"^[a-z0-9_]+(\.[a-z0-9_]+)*$"
PATH_MAX_CHARS = 100


def _ancestors(path: str) -> list[str]:
    """パスの祖先を返す。

    Args:
        path: ドット区切りのパス（`a.b.c`）。

    Returns:
        祖先のパス（`a` と `a.b`）。
    """
    parts = path.split(".")
    return [".".join(parts[:depth]) for depth in range(1, len(parts))]


@dataclass(frozen=True)
class FieldSpec:
    """1 項目の定義。

    機微区分は省略できない（区分の付け忘れを、既定値で黙って通さない）。

    Attributes:
        path: 正規ドメインモデル上のパス（`shipping.address.city`）。葉の項目だけを載せる。
        type: JSON のスカラーの型名（`FIELD_TYPES`: string / number / integer / boolean）。
            配列の中の項目は要素の型を書く。
        description: 意味・単位・書式。モデルが読む。
        sensitivity: 機微区分（`SENSITIVITY_LEVELS` のいずれか）。
        views: この項目を含むビュー（`summary` / `detail`）。`personal` の項目は入れない。
        in_search: `*_search` の `fields` で指定できるか。
    """

    path: str
    type: str
    description: str
    sensitivity: str
    views: tuple[str, ...] = ()
    in_search: bool = False


class Catalog:
    """1 エンティティの項目カタログ。"""

    def __init__(self, entity: str, fields: Sequence[FieldSpec]) -> None:
        """項目の定義を登録する。

        Args:
            entity: エンティティ名（`fields_describe` の `entity` の値）。
            fields: 項目の定義。並び順がそのまま説明とビューの順になる。

        Raises:
            ValueError: パスが書式（`PATH_PATTERN`）か長さ（`PATH_MAX_CHARS`）の上限を外れる・重複している・
                型が JSON のスカラーでない・機微区分が未知・個人情報（personal）の項目をビューに入れた・
                あるパスが別のパスの祖先になっているとき。
        """
        self.entity = entity
        self._fields = list(fields)
        self._index = {spec.path: spec for spec in self._fields}
        malformed = [
            spec.path
            for spec in self._fields
            if len(spec.path) > PATH_MAX_CHARS or not re.fullmatch(PATH_PATTERN, spec.path)
        ]
        if malformed:
            raise ValueError(
                f"{entity}: {malformed} はパスの書式（{PATH_PATTERN}、{PATH_MAX_CHARS} 文字以下）に合わない"
            )
        if len(self._index) != len(self._fields):
            raise ValueError(f"{entity}: 項目のパスが重複している")
        non_scalar = [spec.path for spec in self._fields if spec.type not in FIELD_TYPES]
        if non_scalar:
            raise ValueError(f"{entity}: {non_scalar} の型を JSON のスカラー（{' / '.join(FIELD_TYPES)}）にする")
        unknown = sorted({spec.sensitivity for spec in self._fields} - set(SENSITIVITY_LEVELS))
        if unknown:
            raise ValueError(f"{entity}: 未知の機微区分 {unknown}")
        personal_in_views = [spec.path for spec in self._fields if spec.sensitivity == "personal" and spec.views]
        if personal_in_views:
            raise ValueError(f"{entity}: personal の項目 {personal_in_views} をビューに入れない（fields で明示させる）")
        ancestors = sorted({ancestor for spec in self._fields for ancestor in _ancestors(spec.path)} & set(self._index))
        if ancestors:
            # 祖先を指定されると、その下の全体（区分の違う子を含む）が返ってしまう
            raise ValueError(f"{entity}: {ancestors} は別の項目の祖先になっている。カタログには葉だけを載せる")

    def view(self, name: str, *, allowed_sensitivity: Collection[str]) -> list[str]:
        """ビューに含まれる項目のうち、主体に返してよい機微区分のもののパスを返す。

        Args:
            name: ビュー名。
            allowed_sensitivity: 呼び出しの主体に返してよい機微区分。

        Returns:
            パスの一覧（定義順）。
        """
        return [spec.path for spec in self._fields if name in spec.views and spec.sensitivity in allowed_sensitivity]

    def summary_view(self, id_path: str, *, allowed_sensitivity: Collection[str]) -> list[str]:
        """検索結果の 1 件に出す項目（summary ビューを、主体に返してよい機微区分で絞ったもの）を返す。

        検索結果の要約も `*_get` と同じく区分で絞る（項目を固定すると、区分の検査を素通りする）。

        Args:
            id_path: 候補を識別する項目のパス（`*_get` に渡す ID）。summary ビューに入れておく。
            allowed_sensitivity: 呼び出しの主体に返してよい機微区分。

        Returns:
            パスの一覧（定義順）。

        Raises:
            ValueError: id_path が summary ビューに無いとき（カタログの定義の誤り）。
            FacadeError: id_path の区分を主体に返せないとき（識別できない候補は返さない）。
        """
        spec = self._index.get(id_path)
        if spec is None or "summary" not in spec.views:
            raise ValueError(f"{self.entity}: {id_path} を summary ビューに入れる（検索結果の候補の識別に使う）")
        if spec.sensitivity not in allowed_sensitivity:
            raise FacadeError(f"{id_path!r} は閲覧の権限が無いため、検索結果を返せません。")
        return self.view("summary", allowed_sensitivity=allowed_sensitivity)

    def personal_paths(self, paths: Sequence[str]) -> list[str]:
        """与えたパスのうち、個人情報（personal）の項目のものを返す（監査ログに残す対象）。

        Args:
            paths: カタログにあるパス（`view` か `resolve` で得たもの）。

        Returns:
            個人情報のパス（与えた順）。
        """
        return [path for path in paths if self._index[path].sensitivity == "personal"]

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
