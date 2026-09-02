"""mem サブシステムのデータクラス定義群（database.py から分離）。

``repos`` / ``sessions`` / ``knowledge`` の 3 テーブルに 1 対 1 対応する
dataclass を定義し、``sqlite3.Row`` からの復元を ``from_row`` で提供する。
タイムスタンプはすべて TEXT ISO8601（UTC）で保持する。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime


def utc_now_iso() -> str:
    """現在時刻を秒精度の ISO8601（UTC）文字列で返す。

    Returns:
        ``2026-08-09T12:34:56+00:00`` 形式の文字列。
    """
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class Repo:
    """リポジトリ台帳 1 行。知識のスコープ境界を表す。

    Attributes:
        id: 人間可読スラッグ（例 ``ple4-dev``）。
        identity_key: 正規化 remote URL、無ければ repo root 絶対パス。
        root_path: 最後に観測した絶対パス。
        remote_url: userinfo（`user:token@`）除去済みの remote URL（無ければ
            None。§7-4 対応、`repo_identity._strip_userinfo` を参照）。
        first_seen_at: 初回観測時刻（ISO8601）。
        last_seen_at: 最終観測時刻（ISO8601）。
    """

    id: str
    identity_key: str
    root_path: str
    remote_url: str | None = None
    first_seen_at: str = field(default_factory=utc_now_iso)
    last_seen_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Repo:
        """``repos`` の Row を Repo に変換する。

        Args:
            row: ``SELECT * FROM repos`` で得た行。

        Returns:
            復元された Repo。
        """
        return cls(
            id=row["id"],
            identity_key=row["identity_key"],
            root_path=row["root_path"],
            remote_url=row["remote_url"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )


@dataclass
class Session:
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

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Session:
        """``sessions`` の Row を Session に変換する。

        Args:
            row: ``SELECT * FROM sessions`` で得た行。

        Returns:
            復元された Session。
        """
        return cls(
            session_uid=row["session_uid"],
            repo_id=row["repo_id"],
            harness=row["harness"],
            handoff=row["handoff"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            id=row["id"],
        )


@dataclass
class Knowledge:
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
        status: ``active`` / ``pending`` / ``archived``。既定値 ``active`` は
            dataclass の形式上の初期値に過ぎず、実際の既定は
            ``knowledge_input.parse_knowledge_payload`` が ``source`` に応じて
            決める（``human`` は ``active``、``agent``/``observer`` は
            ``pending``）。``KnowledgeDraft.to_knowledge`` は常にこの解決済み
            値を渡すため、この dataclass 初期値が実際に使われることはない。
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
    repo_id: str | None = None
    body: str = ""
    domain: str | None = None
    confidence: float = 0.5
    status: str = "active"  # 実際の既定は knowledge_input.py 参照（未使用）。
    source_ref: str | None = None
    session_id: int | None = None
    superseded_by: int | None = None
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    id: int | None = None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> Knowledge:
        """``knowledge`` の Row を Knowledge に変換する。

        Args:
            row: ``SELECT * FROM knowledge`` で得た行。

        Returns:
            復元された Knowledge。
        """
        return cls(
            key=row["key"],
            scope=row["scope"],
            kind=row["kind"],
            title=row["title"],
            source=row["source"],
            repo_id=row["repo_id"],
            body=row["body"],
            domain=row["domain"],
            confidence=row["confidence"],
            status=row["status"],
            source_ref=row["source_ref"],
            session_id=row["session_id"],
            superseded_by=row["superseded_by"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            id=row["id"],
        )
