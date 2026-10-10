"""mem サブシステムのデータクラス定義群（database.py から分離）。

``repos`` / ``sessions`` / ``knowledge`` の 3 テーブルに 1 対 1 対応する
dataclass を定義し、``sqlite3.Row`` からの復元を ``from_row`` で提供する。
タイムスタンプはすべて TEXT ISO8601（UTC）で保持する。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Self


def utc_now_iso() -> str:
    """現在時刻を秒精度の ISO8601（UTC）文字列で返す。

    Returns:
        ``2026-08-09T12:34:56+00:00`` 形式の文字列。
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()


class _RowModel:
    """テーブルの列名と同名のフィールドを持つ dataclass の基底。"""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Self:
        """``SELECT *`` で得た行を、同名フィールドへ写して復元する。

        Args:
            row: 対応するテーブルの行。

        Returns:
            復元したインスタンス。
        """
        return cls(**{f.name: row[f.name] for f in fields(cls)})


@dataclass
class Repo(_RowModel):
    """リポジトリ台帳 1 行。知識のスコープ境界を表す。

    Attributes:
        id: 人間可読スラッグ（例 ``claq-dev``）。
        identity_key: 正規化 remote URL、無ければ repo root 絶対パス。
        root_path: 最後に観測した絶対パス。
        remote_url: credential（userinfo・query・fragment）除去済みの remote URL
            （無ければ None。`repo_identity._strip_credentials` を参照）。
        first_seen_at: 初回観測時刻（ISO8601）。
        last_seen_at: 最終観測時刻（ISO8601）。
    """

    id: str
    identity_key: str
    root_path: str
    remote_url: str | None = None
    first_seen_at: str = field(default_factory=utc_now_iso)
    last_seen_at: str = field(default_factory=utc_now_iso)


@dataclass
class Session(_RowModel):
    """セッション履歴 1 行。knowledge の出所であり引き継ぎを持つ。

    Attributes:
        session_uid: ハーネスが渡す session_id。
        repo_id: 所属リポジトリの ``repos.id``。
        harness: ``claude`` / ``codex`` / ``copilot`` / ``unknown``。
        handoff: 次セッションへの引き継ぎ（人間可読の散文）。
        started_at: 開始時刻（ISO8601）。
        ended_at: 終了時刻（ISO8601）。未終了なら None。
        id: 採番済みの主キー。未保存なら None。
    """

    session_uid: str
    repo_id: str
    harness: str = "unknown"
    handoff: str = ""
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    id: int | None = None


@dataclass
class Knowledge(_RowModel):
    """知識カード 1 行。1 行 = 1 つの再利用可能な言明。

    Attributes:
        key: kebab-case スラッグ。重複投入の防止キー。
        scope: ``global`` または ``repo``。
        kind: ``convention`` / ``decision`` / ``pitfall`` / ``howto`` /
            ``fact`` / ``preference``。
        title: 1 行。これ単体で意味が通ること。
        source: ``agent`` / ``observer`` / ``human``。
        repo_id: ``scope='repo'`` のとき必須、``global`` のとき None。
        body: why / how の補足。空でよい。
        domain: ``testing`` ``git`` ``build`` 等。任意。
        confidence: 0.0〜1.0 の確信度。
        status: ``active`` / ``pending`` / ``archived``。**既定値を持たない
            必須引数**にしてある。``"active"`` を既定に持つと、``Knowledge(...)`` を
            直接構築する経路が 1 本増えた瞬間に、「agent 由来カードは人間の
            promote を経なければ注入されない」原則を素通りする ``active`` カードが
            生まれる。呼び出し側に毎回明示させることでその経路を作らせない。
            generic learn 経路の値は ``knowledge_input`` が ``pending`` に固定する。
        source_ref: 出所の自由記述（ファイルパス等）。
        session_id: 出所セッションの ``sessions.id``。
        superseded_by: この行を置き換えた ``knowledge.id``。
        created_at: 作成時刻（ISO8601）。
        updated_at: 更新時刻（ISO8601）。
        id: 採番済みの主キー。未保存なら None。
    """

    key: str
    scope: str
    kind: str
    title: str
    source: str
    status: str
    repo_id: str | None = None
    body: str = ""
    domain: str | None = None
    confidence: float = 0.5
    source_ref: str | None = None
    session_id: int | None = None
    superseded_by: int | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    id: int | None = None
