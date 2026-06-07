"""
SQLite ベースの state store を公開します。
マイグレーション適用済みの接続を包み、セッションやスキル実行などのクエリ API をまとめて扱います。
メモリ DB とファイル DB の両方に対応します。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .migrations import apply_migrations, get_applied_migrations
from .queries import (
    Decision,
    GovernanceEvent,
    InstallStateRecord,
    QueryApi,
    Session,
    SkillRun,
    SkillVersion,
)


class StateStore:
    """SQLite を使用する state store。"""

    def __init__(self, conn: sqlite3.Connection, db_path: str | None = None):
        """StateStore を初期化する。

        Args:
            conn: conn の値
            db_path: db のパス

        Returns:
            None: 値を返しません。

        Raises:
            例外は発生しません。
        """
        self._conn = conn
        self._db_path = db_path
        self._query_api = QueryApi(conn)
        self._closed = False

    @property
    def db_path(self) -> str | None:
        """データベースパスを取得する。

        Returns:
            str | None: str を返します。見つからない場合は None です。

        Args:
            self: このインスタンスです。

        Raises:
            例外は発生しません。
        """
        return self._db_path

    @property
    def is_memory(self) -> bool:
        """インメモリデータベースを使用中か確認する。

        Returns:
            bool: 条件を満たす場合は True、そうでない場合は False。

        Args:
            self: このインスタンスです。

        Raises:
            例外は発生しません。
        """
        return self._db_path is None or self._db_path == ":memory:"

    def close(self) -> None:
        """データベース接続を閉じる。

        Returns:
            None: 値を返しません。

        Args:
            self: このインスタンスです。

        Raises:
            例外は発生しません。
        """
        if not self._closed:
            self._conn.close()
            self._closed = True

    def save(self) -> None:
        """インメモリデータベースをディスクへ保存する（db_path が設定されている場合）。

        Returns:
            None: 値を返しません。

        Args:
            self: このインスタンスです。

        Raises:
            例外は発生しません。
        """
        if self._db_path and self._db_path != ":memory:":
            # ファイルベースのデータベースでは commit で十分
            self._conn.commit()

    # セッション操作
    def get_session_by_id(self, session_id: str) -> Session | None:
        """ID でセッションを取得する。

        Args:
            session_id: セッションID

        Returns:
            Session | None: Session を返します。見つからない場合は None です。

        Raises:
            例外は発生しません。
        """
        return self._query_api.get_session_by_id(session_id)

    def list_recent_sessions(self, limit: int = 10) -> dict:
        """最近のセッション一覧を取得する。

        Args:
            limit: 返す件数の上限

        Returns:
            dict: 一覧を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.list_recent_sessions(limit)

    def get_session_detail(self, session_id: str) -> dict | None:
        """セッションの詳細情報を取得する。

        Args:
            session_id: セッションID

        Returns:
            dict | None: dict を返します。見つからない場合は None です。

        Raises:
            例外は発生しません。
        """
        return self._query_api.get_session_detail(session_id)

    def upsert_session(self, session: dict) -> Session | None:
        """セッションを挿入または更新する。

        Args:
            session: session の値

        Returns:
            Session | None: Session を返します。見つからない場合は None です。

        Raises:
            例外は発生しません。
        """
        return self._query_api.upsert_session(session)

    # スキル実行操作
    def insert_skill_run(self, skill_run: dict) -> SkillRun:
        """スキル実行を挿入する。

        Args:
            skill_run: skill_run の値

        Returns:
            SkillRun: 処理結果を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.insert_skill_run(skill_run)

    # スキルバージョン操作
    def upsert_skill_version(self, skill_version: dict) -> SkillVersion | None:
        """スキルバージョンを挿入または更新する。

        Args:
            skill_version: skill_version の値

        Returns:
            SkillVersion | None: SkillVersion を返します。見つからない場合は None です。

        Raises:
            例外は発生しません。
        """
        return self._query_api.upsert_skill_version(skill_version)

    # 意思決定操作
    def insert_decision(self, decision: dict) -> Decision:
        """意思決定を挿入する。

        Args:
            decision: decision の値

        Returns:
            Decision: 処理結果を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.insert_decision(decision)

    # インストール状態操作
    def upsert_install_state(self, install_state: dict) -> InstallStateRecord:
        """インストール状態を挿入または更新する。

        Args:
            install_state: インストール状態

        Returns:
            InstallStateRecord: 処理結果を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.upsert_install_state(install_state)

    # ガバナンスイベント操作
    def insert_governance_event(self, event: dict) -> GovernanceEvent:
        """ガバナンスイベントを挿入する。

        Args:
            event: event の値

        Returns:
            GovernanceEvent: 処理結果を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.insert_governance_event(event)

    # ステータス操作
    def get_status(self, **kwargs: Any) -> dict:
        """全体ステータスを取得する。

        Args:
            kwargs: 追加のキーワード引数

        Returns:
            dict: 取得結果を返します。

        Raises:
            例外は発生しません。
        """
        return self._query_api.get_status(**kwargs)


def create_state_store(
    db_path: str | Path | None = None,
    *,
    auto_migrate: bool = True,
) -> StateStore:
    """state store を作成する。

    Args:
        db_path: db のパス
        auto_migrate: auto_migrate の値

    Returns:
        StateStore: 作成結果を返します。

    Raises:
        例外は発生しません。
    """
    if db_path is None:
        db_path_str = ":memory:"
    else:
        db_path_str = str(db_path)
        # ファイルベース DB 用に親ディレクトリが存在することを保証
        if db_path_str != ":memory:":
            Path(db_path_str).parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(db_path_str)
    conn.execute("PRAGMA foreign_keys = ON")

    if auto_migrate:
        apply_migrations(conn)

    return StateStore(conn, db_path_str if db_path_str != ":memory:" else None)


__all__ = [
    "Decision",
    "GovernanceEvent",
    "InstallStateRecord",
    "QueryApi",
    "Session",
    "SkillRun",
    "SkillVersion",
    "StateStore",
    "apply_migrations",
    "create_state_store",
    "get_applied_migrations",
]
