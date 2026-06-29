"""SQLite データベース管理 (FTS5 trigram + sqlite-vec)"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import time
from contextlib import contextmanager
from pathlib import Path

from bluecore.mem.logger import get as _get_logger
from bluecore.mem.models import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
    generate_uuid,
)
from bluecore.mem.row_converters import (
    _row_to_adr,
    _row_to_chunk,
    _row_to_event_log,
    _row_to_instinct,
    _row_to_interaction_log,
    _row_to_mem_item_run,
    _row_to_project_profile,
)
from bluecore.mem.schema import _FTS5_SQL, _MIGRATIONS, _SCHEMA_SQL, _VEC_SQL

log = _get_logger("DB")

# 並列 async hook 競合時の chunk_index リトライ上限。テストからパッチ可能なモジュール定数。
_STORE_CHUNK_MAX_RETRIES = 5


def _make_prompt_hash(prompt: str) -> str:
    """プロンプトの SHA256 先頭16文字を返す（重複検出用）。"""
    return hashlib.sha256(prompt.encode()).hexdigest()[:16]


class Database:
    """SQLite による永続メモリストア（FTS5 trigram + sqlite-vec）。"""

    def __init__(self, db_path: str | Path) -> None:
        """DB へ接続し、スキーマ初期化・マイグレーション・最適化 PRAGMA を適用する。"""
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _existed = path.exists()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        if not _existed:
            path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()
        self._migrate()
        # Phase 0: パフォーマンス最適化 PRAGMA
        self.conn.execute("PRAGMA temp_store = MEMORY")
        self.conn.execute("PRAGMA mmap_size = 268435456")
        self.conn.execute("PRAGMA cache_size = -64000")

    def _init_schema(self) -> None:
        """基本スキーマ・FTS5・sqlite-vec（任意）を作成する。"""
        cur = self.conn.cursor()
        cur.executescript(_SCHEMA_SQL)

        # FTS5 (SQLite 組み込み)
        try:
            cur.executescript(_FTS5_SQL)
        except sqlite3.OperationalError as e:
            log.warning("FTS5 初期化失敗（古い SQLite?）: %s", e)

        self.vec_enabled = self._init_vec(cur)
        self.conn.commit()

    def _init_vec(self, cur: sqlite3.Cursor) -> bool:
        """sqlite-vec 拡張をロードし vec テーブルを作成する。

        利用可否を返す。拡張パッケージ未導入（ImportError）や、拡張ロードを
        サポートしない Python（enable_load_extension が無い pyenv ビルド等で
        AttributeError / sqlite3.Error）では False を返し、ベクトル検索を
        無効化して FTS5 のみで縮退する。store_embeddings / vec_search は
        このフラグでガードし、テーブル不在による例外（no such table）を防ぐ。
        SQLite は既定で拡張ロードを禁止しているため enable_load_extension で
        一時的に許可し、拡張ロード後は再度禁止してセキュリティ縮小する。

        Args:
            cur: スキーマ適用に使うカーソル。

        Returns:
            sqlite-vec が利用可能で vec テーブルを作成できた場合 True。
        """
        try:
            import sqlite_vec  # type: ignore[import-untyped]

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            cur.executescript(_VEC_SQL)
            self.conn.enable_load_extension(False)
            return True
        except ImportError:
            log.debug("sqlite-vec は利用できません（ベクトル検索は無効）")
            return False
        except (AttributeError, sqlite3.Error) as e:
            # enable_load_extension 非対応 Python・拡張ロード禁止ビルド等。
            # 恒久的な環境特性であり ImportError 同様 debug で静かに縮退する。
            log.debug("sqlite-vec ロード失敗（ベクトル検索は無効）: %s", e)
            return False

    def _migrate(self) -> None:
        """マイグレーション管理テーブル（schema_migrations は _SCHEMA_SQL で作成済み）を使い、未適用のみ実行する。"""
        applied = {r[0] for r in self.conn.execute("SELECT version FROM schema_migrations").fetchall()}
        for version, sqls in _MIGRATIONS:  # pragma: no cover  # _MIGRATIONS は現状空（本体は将来用）
            if version not in applied:
                for sql in sqls:
                    self.conn.execute(sql)
                self.conn.execute(
                    "INSERT INTO schema_migrations (version, applied_at_epoch) VALUES (?, ?)",
                    (version, int(time.time())),
                )
        self.conn.commit()

    def close(self) -> None:
        """DB 接続を閉じる。"""
        self.conn.close()

    @contextmanager
    def begin_immediate_transaction(self):
        """BEGIN IMMEDIATE トランザクションを開始し、接続を yield する。

        ロック競合時（OperationalError）を含む例外でロールバックする。
        正常終了時にコミットする。
        """
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    # --- セッション ---

    def upsert_session(self, session: Session) -> str:
        """セッションを挿入または更新し、id を返す。"""
        if not session.id:
            session.id = generate_uuid()

        cur = self.conn.execute(
            """INSERT INTO sessions
         (id, origin_user, session_id, project, started_at_epoch,
          branch, commit_hash, uncommitted_count)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(session_id) DO UPDATE SET
            chunk_count = chunk_count,
            synced_at = NULL
          RETURNING id""",
            (
                session.id,
                session.origin_user,
                session.session_id,
                session.project,
                session.started_at_epoch,
                session.branch,
                session.commit_hash,
                session.uncommitted_count,
            ),
        )
        row = cur.fetchone()
        self.conn.commit()
        return row["id"]

    def end_session(self, session_id: str) -> None:
        """セッション終了時刻を記録する。"""
        self.conn.execute(
            "UPDATE sessions SET ended_at_epoch = ?, synced_at = NULL WHERE session_id = ?",
            (int(time.time()), session_id),
        )
        self.conn.commit()

    # --- チャンク ---

    def _insert_chunk_row(self, chunk: MemoryChunk) -> sqlite3.Row:
        """チャンクを INSERT して (id, chunk_index) 行を返す。トランザクション管理は呼び出し元の責務。"""
        cur = self.conn.execute(
            """INSERT INTO memory_chunks
             (id, origin_user, session_id, project, chunk_index, content,
              tool_names, files_read, files_modified,
              user_prompt, created_at_epoch,
              execution_status, tool_error, ai_response_summary, tool_sequence)
             VALUES (?, ?, ?,
                     ?,
                     COALESCE((SELECT MAX(chunk_index) + 1 FROM memory_chunks WHERE session_id = ?), 0),
                     ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
             RETURNING id, chunk_index""",
            (
                chunk.id,
                chunk.origin_user,
                chunk.session_id,
                chunk.project,
                chunk.session_id,
                chunk.content,
                json.dumps(chunk.tool_names, ensure_ascii=False),
                json.dumps(chunk.files_read, ensure_ascii=False),
                json.dumps(chunk.files_modified, ensure_ascii=False),
                chunk.user_prompt,
                chunk.created_at_epoch,
                chunk.execution_status,
                chunk.tool_error,
                chunk.ai_response_summary,
                json.dumps(chunk.tool_sequence, ensure_ascii=False),
            ),
        )
        return cur.fetchone()

    def store_chunk(self, chunk: MemoryChunk) -> str:
        """チャンクを保存し、生成された id を返す。セッションの chunk_count も同一トランザクションで更新。

        並列の async hook が同一 session_id に同時挿入すると chunk_index の UNIQUE 制約に違反する。
        SQLITE_CONSTRAINT_UNIQUE かつ chunk_index を含む違反のみリトライ対象。
        上限は _STORE_CHUNK_MAX_RETRIES。PRIMARY KEY 違反等の他の IntegrityError は即 raise する。
        """
        if not chunk.id:
            chunk.id = generate_uuid()

        max_retries = _STORE_CHUNK_MAX_RETRIES
        for attempt in range(max_retries):
            try:
                row = self._insert_chunk_row(chunk)
                chunk.chunk_index = row["chunk_index"]
                self.conn.execute(
                    "UPDATE sessions SET chunk_count = chunk_count + 1, synced_at = NULL WHERE session_id = ?",
                    (chunk.session_id,),
                )
                self.conn.commit()
                return row["id"]
            except sqlite3.IntegrityError as e:
                self.conn.rollback()
                if e.sqlite_errorcode != sqlite3.SQLITE_CONSTRAINT_UNIQUE or "chunk_index" not in str(e):
                    raise
                if attempt == max_retries - 1:
                    log.error(
                        "chunk_index 競合 %d/%d 回でも解消不能 session=%s: %s",
                        attempt + 1, max_retries, chunk.session_id, e,
                    )
                    raise
                log.warning(
                    "chunk_index 競合 attempt=%d/%d session=%s: %s",
                    attempt + 1, max_retries, chunk.session_id, e,
                )
        raise AssertionError("unreachable")  # pragma: no cover

    def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
        """セッションのチャンクを chunk_index 順に取得する。"""
        rows = self.conn.execute(
            "SELECT * FROM memory_chunks WHERE session_id = ? ORDER BY chunk_index",
            (session_id,),
        ).fetchall()
        return [_row_to_chunk(r) for r in rows]

    def get_chunk_by_id(self, chunk_id: str) -> MemoryChunk | None:
        """id でチャンクを取得する。存在しなければ None。"""
        row = self.conn.execute("SELECT * FROM memory_chunks WHERE id = ?", (chunk_id,)).fetchone()
        return _row_to_chunk(row) if row else None

    def get_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, MemoryChunk]:
        """複数チャンクを一括取得する（N+1 クエリ回避）。"""
        if not chunk_ids:
            return {}
        placeholders = ",".join("?" * len(chunk_ids))
        rows = self.conn.execute(
            f"SELECT * FROM memory_chunks WHERE id IN ({placeholders})",
            chunk_ids,
        ).fetchall()
        return {r["id"]: _row_to_chunk(r) for r in rows}

    def get_next_chunk_index(self, session_id: str) -> int:
        """セッション内で次に割り当てる chunk_index を返す。"""
        row = self.conn.execute(
            "SELECT MAX(chunk_index) as mx FROM memory_chunks WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        mx = row["mx"]
        return (mx if mx is not None else -1) + 1

    def get_all_chunks(self) -> list[MemoryChunk]:
        """全チャンクを取得する（圧縮・プルーニング用）。"""
        rows = self.conn.execute("SELECT * FROM memory_chunks ORDER BY created_at_epoch").fetchall()
        return [_row_to_chunk(r) for r in rows]

    # --- FTS5 検索 ---

    def fts_search(self, query: str, limit: int = 40) -> list[tuple[str, float]]:
        """FTS5 trigram 検索。(chunk_id, rank) のリストを返す。"""
        try:
            rows = self.conn.execute(
                """SELECT chunk_id, rank
           FROM memory_chunks_fts
           WHERE memory_chunks_fts MATCH ?
           ORDER BY rank
           LIMIT ?""",
                (f'"{query}"', limit),
            ).fetchall()
            return [(r["chunk_id"], r["rank"]) for r in rows]
        except sqlite3.OperationalError as e:
            log.warning("FTS5 検索エラー: %s", e)
            return []

    # --- ベクトル検索 ---

    def store_embeddings(self, chunk_ids: list[str], embeddings: list[list[float]]) -> None:
        """エンべディングを一括保存する。vec 無効環境では何もしない。"""
        if not self.vec_enabled:
            return
        for cid, emb in zip(chunk_ids, embeddings, strict=False):
            blob = struct.pack(f"{len(emb)}f", *emb)
            self.conn.execute(
                "INSERT OR REPLACE INTO memory_chunks_vec (chunk_id, embedding) VALUES (?, ?)",
                (cid, blob),
            )
        self.conn.commit()

    def recreate_vec_table(self) -> bool:
        """memory_chunks_vec を DROP して現行スキーマで再作成する。

        埋め込み次元が変わっても CREATE TABLE IF NOT EXISTS では既存テーブルが更新されないため、
        破棄して現行スキーマで作り直す。

        Returns:
            sqlite-vec が利用可能で再作成した場合 True、利用不可なら False。
        """
        if not self.vec_enabled:
            log.debug("sqlite-vec は利用できません（vec テーブル再作成をスキップ）")
            return False
        self.conn.execute("DROP TABLE IF EXISTS memory_chunks_vec")
        self.conn.executescript(_VEC_SQL)
        self.conn.commit()
        return True

    def vec_search(self, embedding: list[float], limit: int = 40) -> list[tuple[str, float]]:
        """sqlite-vec ベクトル検索。(chunk_id, distance) のリストを返す。"""
        if not self.vec_enabled:
            return []
        try:
            blob = struct.pack(f"{len(embedding)}f", *embedding)
            rows = self.conn.execute(
                """SELECT chunk_id, distance
           FROM memory_chunks_vec
           WHERE embedding MATCH ?
           ORDER BY distance
           LIMIT ?""",
                (blob, limit),
            ).fetchall()
            return [(r["chunk_id"], r["distance"]) for r in rows]
        except Exception as e:
            log.warning("ベクトル検索エラー: %s", e)
            return []

    # --- アクセス追跡 ---

    def update_access(self, chunk_ids: list[str]) -> None:
        """検索でヒットしたチャンクのアクセス情報をバッチ更新する（ベストエフォート）"""
        if not chunk_ids:
            return
        unique_ids = list(dict.fromkeys(chunk_ids))  # 順序保持で重複排除
        now = int(time.time())
        try:
            self.conn.executemany(
                """UPDATE memory_chunks
              SET access_count = access_count + 1,
                 last_accessed_epoch = ?,
                 synced_at = NULL
              WHERE id = ?""",
                [(now, cid) for cid in unique_ids],
            )
            self.conn.commit()
        except Exception as e:
            log.debug("アクセスカウント更新スキップ: %s", e)

    # --- ユーティリティ ---

    def get_recent_chunks(self, limit: int = 50, project: str | None = None) -> list[MemoryChunk]:
        """最新のチャンクを取得する。コンテキスト注入用。"""
        if project:
            rows = self.conn.execute(
                "SELECT * FROM memory_chunks WHERE project = ? ORDER BY created_at_epoch DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM memory_chunks ORDER BY created_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_chunk(r) for r in rows]

    # --- インスティンクト ---

    def upsert_instinct(self, instinct: Instinct) -> str:
        """インスティンクトを保存または更新し、id を返す。"""
        instinct_uuid = instinct.id or generate_uuid()
        self.conn.execute(
            """INSERT INTO instincts
         (id, origin_user, instinct_id, scope, project_id, trigger_text,
          confidence, domain, content, created_at_epoch, updated_at_epoch,
          observation_count, confidence_reasons, source_interaction_ids, last_activated_epoch)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(origin_user, instinct_id, scope, project_id) DO UPDATE SET
            trigger_text = excluded.trigger_text,
            confidence = excluded.confidence,
            domain = excluded.domain,
            content = excluded.content,
            updated_at_epoch = excluded.updated_at_epoch,
            observation_count = excluded.observation_count,
            confidence_reasons = excluded.confidence_reasons,
            source_interaction_ids = excluded.source_interaction_ids,
            last_activated_epoch = excluded.last_activated_epoch,
            synced_at = NULL""",
            (
                instinct_uuid,
                instinct.origin_user,
                instinct.instinct_id,
                instinct.scope,
                instinct.project_id,
                instinct.trigger_text,
                instinct.confidence,
                instinct.domain,
                instinct.content,
                instinct.created_at_epoch,
                instinct.updated_at_epoch,
                instinct.observation_count,
                json.dumps(instinct.confidence_reasons, ensure_ascii=False),
                json.dumps(instinct.source_interaction_ids, ensure_ascii=False),
                instinct.last_activated_epoch,
            ),
        )
        self.conn.commit()
        return instinct_uuid

    def get_instincts(self, scope: str | None = None, project_id: str | None = None) -> list[Instinct]:
        """インスティンクトを取得する。"""
        if scope and project_id:
            rows = self.conn.execute(
                "SELECT * FROM instincts WHERE scope = ? AND project_id = ?",
                (scope, project_id),
            ).fetchall()
        elif scope:
            rows = self.conn.execute("SELECT * FROM instincts WHERE scope = ?", (scope,)).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM instincts").fetchall()
        return [_row_to_instinct(r) for r in rows]

    def get_all_instincts(self) -> list[Instinct]:
        """全インスティンクトを取得する（同期用）。"""
        rows = self.conn.execute("SELECT * FROM instincts ORDER BY created_at_epoch").fetchall()
        return [_row_to_instinct(r) for r in rows]

    # --- ADR ---

    def upsert_adr(self, adr: Adr) -> str:
        """ADR を保存または更新し、id を返す。"""
        adr_uuid = adr.id or generate_uuid()
        self.conn.execute(
            """INSERT INTO adrs
         (id, origin_user, project, adr_number, title, status, content,
          created_at_epoch, updated_at_epoch)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(origin_user, project, adr_number) DO UPDATE SET
            title = excluded.title,
            status = excluded.status,
            content = excluded.content,
            updated_at_epoch = excluded.updated_at_epoch,
            synced_at = NULL""",
            (
                adr_uuid,
                adr.origin_user,
                adr.project,
                adr.adr_number,
                adr.title,
                adr.status,
                adr.content,
                adr.created_at_epoch,
                adr.updated_at_epoch,
            ),
        )
        self.conn.commit()
        return adr_uuid

    def get_adrs(self, project: str | None = None) -> list[Adr]:
        """ADR を取得する。"""
        if project:
            rows = self.conn.execute(
                "SELECT * FROM adrs WHERE project = ? ORDER BY adr_number",
                (project,),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM adrs ORDER BY project, adr_number").fetchall()
        return [_row_to_adr(r) for r in rows]

    def get_all_adrs(self) -> list[Adr]:
        """全 ADR を取得する（同期用）。"""
        rows = self.conn.execute("SELECT * FROM adrs ORDER BY created_at_epoch").fetchall()
        return [_row_to_adr(r) for r in rows]

    # --- イベントログ ---

    def store_event_log(self, event: EventLog) -> str:
        """イベントログを保存し、id を返す。"""
        event_uuid = event.id or generate_uuid()
        self.conn.execute(
            """INSERT OR IGNORE INTO event_logs
         (id, origin_user, event_type, project_id, content, created_at_epoch)
         VALUES (?, ?, ?, ?, ?, ?)""",
            (
                event_uuid,
                event.origin_user,
                event.event_type,
                event.project_id,
                event.content,
                event.created_at_epoch,
            ),
        )
        self.conn.commit()
        return event_uuid

    def get_event_logs(self, event_type: str | None = None, limit: int = 100) -> list[EventLog]:
        """イベントログを取得する。"""
        if event_type:
            rows = self.conn.execute(
                "SELECT * FROM event_logs WHERE event_type = ? ORDER BY created_at_epoch DESC LIMIT ?",
                (event_type, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM event_logs ORDER BY created_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_event_log(r) for r in rows]

    def get_all_event_logs(self) -> list[EventLog]:
        """全イベントログを取得する（同期用）。"""
        rows = self.conn.execute("SELECT * FROM event_logs ORDER BY created_at_epoch").fetchall()
        return [_row_to_event_log(r) for r in rows]

    # --- インタラクションログ ---

    def store_interaction_log(self, log_entry: InteractionLog) -> str:
        """インタラクションログを保存し、id を返す。"""
        log_uuid = log_entry.id or generate_uuid()
        prompt_hash = log_entry.user_prompt_hash or _make_prompt_hash(log_entry.user_prompt_full)
        self.conn.execute(
            """INSERT OR IGNORE INTO interaction_logs
         (id, origin_user, session_id, project,
          user_prompt_full, user_prompt_hash,
          ai_response_summary, ai_response_tool_plan,
          chunk_id, execution_outcome, tool_error_count,
          interaction_index, created_at_epoch)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                log_uuid,
                log_entry.origin_user,
                log_entry.session_id,
                log_entry.project,
                log_entry.user_prompt_full,
                prompt_hash,
                log_entry.ai_response_summary,
                log_entry.ai_response_tool_plan,
                log_entry.chunk_id,
                log_entry.execution_outcome,
                log_entry.tool_error_count,
                log_entry.interaction_index,
                log_entry.created_at_epoch,
            ),
        )
        self.conn.commit()
        return log_uuid

    def get_interaction_logs(
        self,
        session_id: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[InteractionLog]:
        """インタラクションログを取得する。"""
        if session_id:
            rows = self.conn.execute(
                "SELECT * FROM interaction_logs WHERE session_id = ? ORDER BY interaction_index LIMIT ?",
                (session_id, limit),
            ).fetchall()
        elif project:
            rows = self.conn.execute(
                "SELECT * FROM interaction_logs WHERE project = ? ORDER BY created_at_epoch DESC LIMIT ?",
                (project, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM interaction_logs ORDER BY created_at_epoch DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_interaction_log(r) for r in rows]

    def get_all_interaction_logs(self) -> list[InteractionLog]:
        """全インタラクションログを取得する（同期用）。"""
        rows = self.conn.execute(
            "SELECT * FROM interaction_logs ORDER BY created_at_epoch"
        ).fetchall()
        return [_row_to_interaction_log(r) for r in rows]

    def get_next_interaction_index(self, session_id: str) -> int:
        """セッション内の次の interaction_index を返す。"""
        row = self.conn.execute(
            "SELECT MAX(interaction_index) as mx FROM interaction_logs WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        mx = row["mx"]
        return (mx if mx is not None else -1) + 1

    # --- プロジェクトプロファイル ---

    def upsert_project_profile(self, profile: ProjectProfile) -> str:
        """プロジェクトプロファイルを保存または更新し、id を返す。"""
        profile_uuid = profile.id or generate_uuid()
        self.conn.execute(
            """INSERT INTO project_profiles
         (id, origin_user, project, project_path,
          languages, frameworks, primary_language,
          test_command, build_command, scope_hint,
          detected_at_epoch, last_updated_epoch, detection_confidence)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
         ON CONFLICT(origin_user, project) DO UPDATE SET
            project_path = excluded.project_path,
            languages = excluded.languages,
            frameworks = excluded.frameworks,
            primary_language = excluded.primary_language,
            test_command = excluded.test_command,
            build_command = excluded.build_command,
            scope_hint = excluded.scope_hint,
            last_updated_epoch = excluded.last_updated_epoch,
            detection_confidence = excluded.detection_confidence,
            synced_at = NULL""",
            (
                profile_uuid,
                profile.origin_user,
                profile.project,
                profile.project_path,
                json.dumps(profile.languages, ensure_ascii=False),
                json.dumps(profile.frameworks, ensure_ascii=False),
                profile.primary_language,
                profile.test_command,
                profile.build_command,
                profile.scope_hint,
                profile.detected_at_epoch,
                profile.last_updated_epoch,
                profile.detection_confidence,
            ),
        )
        self.conn.commit()
        return profile_uuid

    def get_project_profile(self, project: str, origin_user: str = "") -> ProjectProfile | None:
        """プロジェクトプロファイルを取得する。"""
        if origin_user:
            row = self.conn.execute(
                "SELECT * FROM project_profiles WHERE project = ? AND origin_user = ?",
                (project, origin_user),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT * FROM project_profiles WHERE project = ? ORDER BY last_updated_epoch DESC LIMIT 1",
                (project,),
            ).fetchone()
        return _row_to_project_profile(row) if row else None

    def get_all_project_profiles(self) -> list[ProjectProfile]:
        """全プロジェクトプロファイルを取得する（同期用）。"""
        rows = self.conn.execute(
            "SELECT * FROM project_profiles ORDER BY last_updated_epoch"
        ).fetchall()
        return [_row_to_project_profile(r) for r in rows]

    # --- スキル実行記録 ---

    def store_mem_item_run(self, run: MemItemRun) -> str:
        """アイテム実行記録を保存し、id を返す。"""
        run_uuid = run.id or generate_uuid()
        self.conn.execute(
            """INSERT INTO mem_item_runs
         (id, origin_user, session_id, project,
          skill_name, skill_trigger, outcome,
          tools_used, files_modified_count, duration_seconds,
          interaction_log_id, created_at_epoch, item_type)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_uuid,
                run.origin_user,
                run.session_id,
                run.project,
                run.skill_name,
                run.skill_trigger,
                run.outcome,
                json.dumps(run.tools_used, ensure_ascii=False),
                run.files_modified_count,
                run.duration_seconds,
                run.interaction_log_id,
                run.created_at_epoch,
                run.item_type,
            ),
        )
        self.conn.commit()
        return run_uuid

    def get_skill_run_stats(
        self,
        skill_name: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[MemItemRun]:
        """スキル実行記録を取得する。"""
        conditions = []
        params: list = []
        if skill_name:
            conditions.append("skill_name = ?")
            params.append(skill_name)
        if project:
            conditions.append("project = ?")
            params.append(project)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)
        rows = self.conn.execute(
            f"SELECT * FROM mem_item_runs {where} ORDER BY created_at_epoch DESC LIMIT ?",
            params,
        ).fetchall()
        return [_row_to_mem_item_run(r) for r in rows]

    def get_all_mem_item_runs(self) -> list[MemItemRun]:
        """全アイテム実行記録を取得する（同期用）。"""
        rows = self.conn.execute(
            "SELECT * FROM mem_item_runs ORDER BY created_at_epoch"
        ).fetchall()
        return [_row_to_mem_item_run(r) for r in rows]
