"""SQLite データベース管理 — repos / sessions / knowledge の 3 テーブル。"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import TracebackType

from bluecore.mem.models import Knowledge, Repo, Session, utc_now_iso
from bluecore.mem.schema import _SCHEMA_SQL


class Database:
    """``~/.bluecore/mem.db`` を扱う永続メモリストア。

    ``with Database(path) as db:`` で開くと、ブロック終了時に自動で close する。
    """

    def __init__(self, db_path: str | Path) -> None:
        """DB へ接続し、スキーマ初期化と最適化 PRAGMA を適用する。

        DB ファイルを新規作成した場合のみパーミッションを 0600 に絞る。

        Args:
            db_path: mem.db のパス。親ディレクトリが無ければ作成する。
        """
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _existed = path.exists()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        if not _existed:
            path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self.conn.execute("PRAGMA mmap_size = 268435456")
        self.conn.execute("PRAGMA cache_size = -64000")

    def _init_schema(self) -> None:
        """repos → sessions → knowledge の順にスキーマを作成する。"""
        self.conn.executescript(_SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        """DB 接続を閉じる。"""
        self.conn.close()

    def __enter__(self) -> Database:
        """コンテキストマネージャとして自身を返す。

        Returns:
            この Database インスタンス。
        """
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """コンテキスト終了時に DB 接続を閉じる。

        Args:
            exc_type: 送出された例外の型。無ければ None。
            exc: 送出された例外。無ければ None。
            tb: 例外のトレースバック。無ければ None。
        """
        self.close()

    # --- repos ---

    def upsert_repo(self, repo: Repo) -> Repo:
        """リポジトリ台帳を挿入または更新する。

        衝突解決は ``identity_key`` で行う。既登録なら ``id`` と
        ``first_seen_at`` を保持したまま観測情報だけを更新する。

        Args:
            repo: 登録したいリポジトリ。

        Returns:
            DB に格納された最新状態の Repo。
        """
        row = self.conn.execute(
            """INSERT INTO repos
                 (id, identity_key, root_path, remote_url, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(identity_key) DO UPDATE SET
                 root_path = excluded.root_path,
                 remote_url = excluded.remote_url,
                 last_seen_at = excluded.last_seen_at
               RETURNING *""",
            (
                repo.id,
                repo.identity_key,
                repo.root_path,
                repo.remote_url,
                repo.first_seen_at,
                repo.last_seen_at,
            ),
        ).fetchone()
        self.conn.commit()
        return Repo.from_row(row)

    def get_repo(self, repo_id: str) -> Repo | None:
        """``repos.id`` でリポジトリを取得する。

        Args:
            repo_id: 人間可読スラッグ。

        Returns:
            該当する Repo。存在しなければ None。
        """
        row = self.conn.execute("SELECT * FROM repos WHERE id = ?", (repo_id,)).fetchone()
        return Repo.from_row(row) if row else None

    def list_repos(self) -> list[Repo]:
        """登録済みリポジトリを最終観測の新しい順に返す。

        Returns:
            Repo のリスト。1 件も無ければ空リスト。
        """
        rows = self.conn.execute("SELECT * FROM repos ORDER BY last_seen_at DESC, id").fetchall()
        return [Repo.from_row(r) for r in rows]

    # --- knowledge ---

    def upsert_knowledge(self, knowledge: Knowledge) -> Knowledge:
        """知識カードを挿入または更新する。

        衝突解決は式インデックス ``(COALESCE(repo_id,''), key)`` で行う。
        既存行の ``id`` と ``created_at`` は保持する。

        Args:
            knowledge: 登録したい知識カード。

        Returns:
            DB に格納された最新状態の Knowledge。
        """
        row = self.conn.execute(
            """INSERT INTO knowledge
                 (key, scope, repo_id, kind, title, body, domain, confidence,
                  status, source, source_ref, session_id, superseded_by,
                  created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(COALESCE(repo_id, ''), key) DO UPDATE SET
                 scope = excluded.scope,
                 kind = excluded.kind,
                 title = excluded.title,
                 body = excluded.body,
                 domain = excluded.domain,
                 confidence = excluded.confidence,
                 status = excluded.status,
                 source = excluded.source,
                 source_ref = excluded.source_ref,
                 session_id = excluded.session_id,
                 superseded_by = excluded.superseded_by,
                 updated_at = excluded.updated_at
               RETURNING *""",
            (
                knowledge.key,
                knowledge.scope,
                knowledge.repo_id,
                knowledge.kind,
                knowledge.title,
                knowledge.body,
                knowledge.domain,
                knowledge.confidence,
                knowledge.status,
                knowledge.source,
                knowledge.source_ref,
                knowledge.session_id,
                knowledge.superseded_by,
                knowledge.created_at,
                knowledge.updated_at,
            ),
        ).fetchone()
        self.conn.commit()
        return Knowledge.from_row(row)

    def get_knowledge_by_key(self, key: str, repo_id: str | None = None) -> Knowledge | None:
        """``key`` と所属リポジトリで知識カードを 1 件取得する。

        Args:
            key: kebab-case スラッグ。
            repo_id: repo スコープの所属リポジトリ。global スコープは None。

        Returns:
            該当する Knowledge。存在しなければ None。
        """
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE COALESCE(repo_id, '') = COALESCE(?, '') AND key = ?",
            (repo_id, key),
        ).fetchone()
        return Knowledge.from_row(row) if row else None

    def list_knowledge(
        self,
        scope: str | None = None,
        repo_id: str | None = None,
        status: str | None = None,
    ) -> list[Knowledge]:
        """条件に一致する知識カードを更新の新しい順に返す。

        Args:
            scope: ``global`` / ``repo``。None なら絞り込まない。
            repo_id: 所属リポジトリ。None なら絞り込まない。
            status: ``active`` / ``pending`` / ``archived``。None なら絞り込まない。

        Returns:
            Knowledge のリスト。該当が無ければ空リスト。
        """
        clauses: list[str] = []
        params: list[str] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if repo_id is not None:
            clauses.append("repo_id = ?")
            params.append(repo_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM knowledge{where} ORDER BY updated_at DESC, id DESC",
            params,
        ).fetchall()
        return [Knowledge.from_row(r) for r in rows]

    def set_knowledge_status(self, knowledge_id: int, status: str, updated_at: str | None = None) -> bool:
        """知識カードの status を更新する。

        Args:
            knowledge_id: 対象の ``knowledge.id``。
            status: ``active`` / ``pending`` / ``archived``。
            updated_at: 更新時刻（ISO8601）。省略時は現在時刻。

        Returns:
            対象行が存在して更新できた場合 True、該当なしなら False。
        """
        cur = self.conn.execute(
            "UPDATE knowledge SET status = ?, updated_at = ? WHERE id = ?",
            (status, updated_at or utc_now_iso(), knowledge_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # --- sessions ---

    def start_session(self, session: Session) -> Session:
        """セッションを開始登録する。

        同一 ``session_uid`` の再入は ``harness`` の更新のみ行い、
        ``started_at`` は初回の値を保持する。

        Args:
            session: 開始するセッション。

        Returns:
            DB に格納された最新状態の Session。
        """
        row = self.conn.execute(
            """INSERT INTO sessions
                 (session_uid, repo_id, harness, handoff, started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(session_uid) DO UPDATE SET
                 harness = excluded.harness
               RETURNING *""",
            (
                session.session_uid,
                session.repo_id,
                session.harness,
                session.handoff,
                session.started_at,
                session.ended_at,
            ),
        ).fetchone()
        self.conn.commit()
        return Session.from_row(row)

    def finish_session(self, session_uid: str, handoff: str, ended_at: str | None = None) -> bool:
        """セッションに引き継ぎと終了時刻を記録する。

        Args:
            session_uid: ハーネスが渡した session_id。
            handoff: 次セッションへの引き継ぎ（人間可読の散文）。
            ended_at: 終了時刻（ISO8601）。省略時は現在時刻。

        Returns:
            対象セッションが存在して更新できた場合 True、該当なしなら False。
        """
        cur = self.conn.execute(
            "UPDATE sessions SET handoff = ?, ended_at = ? WHERE session_uid = ?",
            (handoff, ended_at or utc_now_iso(), session_uid),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_latest_session(self, repo_id: str) -> Session | None:
        """引き継ぎを持つ最新セッションを 1 件返す。

        ``handoff`` が空の行は次セッションへ渡すものが無いため除外する。

        Args:
            repo_id: 対象リポジトリの ``repos.id``。

        Returns:
            該当する Session。存在しなければ None。
        """
        row = self.conn.execute(
            """SELECT * FROM sessions
               WHERE repo_id = ? AND handoff != ''
               ORDER BY started_at DESC, id DESC
               LIMIT 1""",
            (repo_id,),
        ).fetchone()
        return Session.from_row(row) if row else None
