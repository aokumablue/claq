"""SQLite → PostgreSQL 同期ロジック"""

from __future__ import annotations

import errno
import fcntl
import sqlite3
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from devgear.lib.core_utils import get_git_user_name
from devgear.mem.database import (
    Database,
    MemoryChunk,
    Session,
    _row_to_adr,
    _row_to_chunk,
    _row_to_event_log,
    _row_to_instinct,
    _row_to_interaction_log,
    _row_to_mem_item_run,
    _row_to_project_profile,
)
from devgear.mem.logger import get as _get_logger
from devgear.mem.pg_database import PgDatabase
from devgear.mem.settings import Settings

log = _get_logger("SYNC")


def _mask_url(url: str) -> str:
    """接続 URL のパスワード部を *** に置換する。@ 含みパスワードにも対応する。"""
    try:
        parsed = urlparse(url)
        if parsed.password is not None:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@", 1)
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return url


# 同期失敗後の最小リトライ間隔（秒）
_MIN_RETRY_INTERVAL = 5 * 60
# 1回の同期で取得する最大行数（メモリ爆発防止）
_SYNC_BATCH_SIZE = 500
# 同期対象テーブル一覧（cli_sync_handlers._count_all_pending でも共有）
# ORDER BY に使用できるカラム名の許可リスト（_claim_pending_rows のセキュリティ検証用）
_ALLOWED_ORDER_BY_COLUMNS: frozenset[str] = frozenset({
    "created_at_epoch",
    "started_at_epoch",
    "last_updated_epoch",
})

_SYNC_TABLES: tuple[str, ...] = (
    "memory_chunks",
    "sessions",
    "instincts",
    "adrs",
    "event_logs",
    "interaction_logs",
    "project_profiles",
    "mem_item_runs",
)


def _row_to_session(row: sqlite3.Row) -> Session:
    """SQLite 行を Session に変換する。"""
    return Session(
        id=row["id"],
        origin_user=row["origin_user"],
        session_id=row["session_id"],
        project=row["project"],
        started_at_epoch=row["started_at_epoch"],
        chunk_count=row["chunk_count"],
        branch=row["branch"],
        commit_hash=row["commit_hash"],
        uncommitted_count=row["uncommitted_count"],
        ended_at_epoch=row["ended_at_epoch"],
        project_profile_id=row["project_profile_id"],
    )


def _count_pending_rows(conn: sqlite3.Connection, table: str) -> int:
    """未同期行数を数える。"""
    if table not in _SYNC_TABLES:
        raise ValueError(f"Invalid table: {table}")
    row = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE synced_at IS NULL").fetchone()
    return int(row[0]) if row else 0


def _claim_pending_rows[T](
    conn: sqlite3.Connection,
    table: str,
    order_by: str,
    synced_at: str,
    row_factory: Callable[[sqlite3.Row], T],
    *,
    batch_size: int = _SYNC_BATCH_SIZE,
) -> list[T]:
    """未同期行を取得して synced_at を立てる（最大 batch_size 件）。"""
    if table not in _SYNC_TABLES:
        raise ValueError(f"Invalid table: {table}")
    if order_by not in _ALLOWED_ORDER_BY_COLUMNS:
        raise ValueError(f"Invalid order_by column: {order_by}")
    rows = conn.execute(
        f"SELECT * FROM {table} WHERE synced_at IS NULL ORDER BY {order_by} LIMIT ?",
        (batch_size,),
    ).fetchall()
    if not rows:
        return []

    conn.executemany(
        f"UPDATE {table} SET synced_at = ? WHERE id = ?",
        [(synced_at, row["id"]) for row in rows],
    )
    return [row_factory(row) for row in rows]


def _count_pending_embeddings(conn: sqlite3.Connection, chunk_ids: list[str]) -> int:
    """未同期チャンクに紐づく埋め込み件数を数える。"""
    if not chunk_ids:
        return 0

    placeholders = ",".join("?" * len(chunk_ids))
    try:
        row = conn.execute(
            f"SELECT COUNT(*) FROM memory_chunks_vec WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _resolve_sync_lock_path(settings: Settings) -> Path:
    """同期ロックファイルのパスを解決する。"""
    lock_path = getattr(settings, "sync_lock_path", None)
    try:
        return Path(lock_path)
    except (TypeError, ValueError):
        return Path.home() / ".devgear" / "sync.lock"


@contextmanager
def _acquire_sync_lock(settings: Settings) -> Iterator[bool]:
    """同期処理用の排他ロックを取得する。"""
    lock_path = _resolve_sync_lock_path(settings)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                yield False
                return
            raise
        try:
            yield True
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _reload_sync_state(settings: Settings) -> None:
    """必要なら sync_state.json を再読み込みする。"""
    reload_sync_state = getattr(settings, "reload_sync_state", None)
    if callable(reload_sync_state):
        reload_sync_state()
        return

    load_sync_state = getattr(settings, "_load_sync_state", None)
    if callable(load_sync_state):
        load_sync_state()


@dataclass
class SyncResult:
    """同期結果"""

    chunks: int = 0
    sessions: int = 0
    instincts: int = 0
    adrs: int = 0
    events: int = 0
    embeddings: int = 0
    interaction_logs: int = 0
    project_profiles: int = 0
    skill_runs: int = 0
    success: bool = True
    error: str | None = None


def sync_to_postgres(
    settings: Settings,
    dry_run: bool = False,
) -> SyncResult:
    """SQLite データを PostgreSQL に同期する。

    Args:
        settings: 設定
        dry_run: True の場合、実際の同期は行わない

    Returns:
        同期結果
    """
    with _acquire_sync_lock(settings) as lock_acquired:
        if not lock_acquired:
            log.info("同期は実行中のためスキップします")
            return SyncResult(success=True)

        _reload_sync_state(settings)
        sync_cfg = settings.sync

        if not sync_cfg.enabled:
            log.info("同期は無効です")
            return SyncResult(success=True)

        if not sync_cfg.postgres_url:
            log.info("同期スキップ: postgres_url 未設定 (~/.devgear/settings.json の mem.sync.postgres_url を設定してください)")
            return SyncResult(success=False, error="postgres_url が設定されていません")

        if not should_sync(settings):
            log.info("同期は最新状態のためスキップします")
            return SyncResult(success=True)

        # 試行開始時刻を実際に同期を始める直前に記録
        sync_cfg.last_sync_attempt_at = time.time()

        sqlite_db: Database | None = None
        pg_db: PgDatabase | None = None
        origin_user = get_git_user_name()

        try:
            sqlite_db = Database(settings.db_path)
            pg_db = PgDatabase(sync_cfg.postgres_url)

            if not pg_db.test_connection():
                sync_cfg.last_sync_success = False
                try:
                    settings.save_sync_state()
                except Exception:
                    pass
                masked_url = _mask_url(sync_cfg.postgres_url)
                log.error("PG 接続失敗: %s", masked_url)
                return SyncResult(success=False, error="PostgreSQL への接続に失敗しました")

            if dry_run:
                log.info("[DRY RUN] 同期をシミュレート中...")
                result = SyncResult(
                    chunks=_count_pending_rows(sqlite_db.conn, "memory_chunks"),
                    sessions=_count_pending_rows(sqlite_db.conn, "sessions"),
                    instincts=_count_pending_rows(sqlite_db.conn, "instincts"),
                    adrs=_count_pending_rows(sqlite_db.conn, "adrs"),
                    events=_count_pending_rows(sqlite_db.conn, "event_logs"),
                    interaction_logs=_count_pending_rows(sqlite_db.conn, "interaction_logs"),
                    project_profiles=_count_pending_rows(sqlite_db.conn, "project_profiles"),
                    skill_runs=_count_pending_rows(sqlite_db.conn, "mem_item_runs"),
                )
                chunk_ids = [
                    row[0]
                    for row in sqlite_db.conn.execute(
                        "SELECT id FROM memory_chunks WHERE synced_at IS NULL ORDER BY created_at_epoch"
                    ).fetchall()
                ]
                result.embeddings = _count_pending_embeddings(sqlite_db.conn, chunk_ids)
                log.info(
                    "[DRY RUN] 同期対象: chunks=%d, sessions=%d, instincts=%d, adrs=%d, "
                    "events=%d, interactions=%d, profiles=%d, skill_runs=%d",
                    result.chunks, result.sessions, result.instincts, result.adrs,
                    result.events, result.interaction_logs, result.project_profiles, result.skill_runs,
                )
                return result

            log.info("PostgreSQL への同期を開始...")

            sync_started_at = datetime.now(UTC).isoformat()
            conn = sqlite_db.conn
            conn.execute("BEGIN IMMEDIATE")
            try:
                chunks = _claim_pending_rows(conn, "memory_chunks", "created_at_epoch", sync_started_at, _row_to_chunk)
                sessions = _claim_pending_rows(conn, "sessions", "started_at_epoch", sync_started_at, _row_to_session)
                instincts = _claim_pending_rows(conn, "instincts", "created_at_epoch", sync_started_at, _row_to_instinct)
                adrs = _claim_pending_rows(conn, "adrs", "created_at_epoch", sync_started_at, _row_to_adr)
                events = _claim_pending_rows(conn, "event_logs", "created_at_epoch", sync_started_at, _row_to_event_log)
                interaction_logs = _claim_pending_rows(
                    conn,
                    "interaction_logs",
                    "created_at_epoch",
                    sync_started_at,
                    _row_to_interaction_log,
                )
                project_profiles = _claim_pending_rows(
                    conn,
                    "project_profiles",
                    "last_updated_epoch",
                    sync_started_at,
                    _row_to_project_profile,
                )
                skill_runs = _claim_pending_rows(
                    conn,
                    "mem_item_runs",
                    "created_at_epoch",
                    sync_started_at,
                    _row_to_mem_item_run,
                )

                if chunks:
                    result = SyncResult(chunks=pg_db.upsert_chunks_batch(chunks, origin_user))
                    log.info("chunks: %d 件同期", result.chunks)
                else:
                    result = SyncResult()

                if sessions:
                    result.sessions = pg_db.upsert_sessions_batch(sessions, origin_user)
                    log.info("sessions: %d 件同期", result.sessions)

                if instincts:
                    for inst in instincts:
                        inst.origin_user = origin_user
                    result.instincts = pg_db.upsert_instincts_batch(instincts)
                    log.info("instincts: %d 件同期", result.instincts)

                if adrs:
                    for adr in adrs:
                        adr.origin_user = origin_user
                    result.adrs = pg_db.upsert_adrs_batch(adrs)
                    log.info("adrs: %d 件同期", result.adrs)

                if events:
                    for ev in events:
                        ev.origin_user = origin_user
                    result.events = pg_db.insert_event_logs_batch(events)
                    log.info("events: %d 件同期", result.events)

                if interaction_logs:
                    for il in interaction_logs:
                        il.origin_user = origin_user
                    result.interaction_logs = pg_db.upsert_interaction_logs_batch(interaction_logs)
                    log.info("interaction_logs: %d 件同期", result.interaction_logs)

                if project_profiles:
                    for pp in project_profiles:
                        pp.origin_user = origin_user
                    result.project_profiles = pg_db.upsert_project_profiles_batch(project_profiles)
                    log.info("project_profiles: %d 件同期", result.project_profiles)

                if skill_runs:
                    for sr in skill_runs:
                        sr.origin_user = origin_user
                    result.skill_runs = pg_db.upsert_mem_item_runs_batch(skill_runs)
                    log.info("skill_runs: %d 件同期", result.skill_runs)

                result.embeddings = _sync_embeddings(sqlite_db, pg_db, chunks)
                if result.embeddings > 0:
                    log.info("embeddings: %d 件同期", result.embeddings)

                conn.commit()
            except Exception:
                conn.rollback()
                raise

            sync_cfg.last_synced_at = time.time()
            sync_cfg.last_sync_success = True
            settings.save_sync_state()

            log.info("同期完了")
            return result

        except Exception as e:
            # 失敗フラグを永続化（暴走防止のリトライ制御に使用）
            sync_cfg.last_sync_success = False
            try:
                settings.save_sync_state()
            except Exception:
                pass
            log.error("同期エラー: %s", e, exc_info=True)
            return SyncResult(success=False, error=_mask_url(str(e)))
        finally:
            if sqlite_db is not None:
                sqlite_db.close()
            if pg_db is not None:
                pg_db.close()


def should_sync(settings: Settings) -> bool:
    """同期が必要かどうかを判定する。

    Args:
        settings: 設定

    Returns:
        True の場合、同期を実行すべき
    """
    sync_cfg = settings.sync

    if not sync_cfg.enabled:
        return False

    if not sync_cfg.postgres_url:
        return False

    now = time.time()
    interval_seconds = sync_cfg.interval_hours * 3600
    next_due_at = sync_cfg.last_synced_at + interval_seconds

    # 前回失敗から MIN_RETRY_INTERVAL 以内は再試行しない（暴走防止）
    if not sync_cfg.last_sync_success and sync_cfg.last_sync_attempt_at > 0:
        if now < sync_cfg.last_sync_attempt_at + _MIN_RETRY_INTERVAL:
            log.debug(
                "同期判定: retry backoff now=%.0f last_attempt=%.0f min_retry=%.0f",
                now,
                sync_cfg.last_sync_attempt_at,
                sync_cfg.last_sync_attempt_at + _MIN_RETRY_INTERVAL,
            )
            return False

    should_run = now >= next_due_at
    log.debug(
        "同期判定: now=%.0f interval_hours=%d last_synced_at=%.0f next_due_at=%.0f "
        "last_sync_attempt_at=%.0f last_sync_success=%s should_run=%s",
        now,
        sync_cfg.interval_hours,
        sync_cfg.last_synced_at,
        next_due_at,
        sync_cfg.last_sync_attempt_at,
        sync_cfg.last_sync_success,
        should_run,
    )
    return should_run


def sync_check(settings: Settings) -> SyncResult:
    """同期間隔をチェックし、必要なら同期を実行する。

    Args:
        settings: 設定

    Returns:
        同期結果（スキップ時は success=True, counts=0）
    """
    if not should_sync(settings):
        sync_cfg = settings.sync
        log.info(
            "同期スキップ: enabled=%s postgres_url_set=%s interval_hours=%d last_synced_at=%.0f "
            "last_sync_attempt_at=%.0f last_sync_success=%s",
            sync_cfg.enabled,
            bool(sync_cfg.postgres_url),
            sync_cfg.interval_hours,
            sync_cfg.last_synced_at,
            sync_cfg.last_sync_attempt_at,
            sync_cfg.last_sync_success,
        )
        return SyncResult(success=True)

    return sync_to_postgres(settings)


def _sync_embeddings(
    sqlite_db: Database,
    pg_db: PgDatabase,
    chunks: list[MemoryChunk],
) -> int:
    """sqlite-vec のエンベディングを pgvector に同期する。

    Args:
        sqlite_db: SQLite データベース
        pg_db: PostgreSQL データベース
        chunks: 同期対象チャンクリスト

    Returns:
        同期したエンベディング数
    """
    if not chunks:
        return 0

    chunk_ids = [str(c.id) for c in chunks]
    embeddings: list[tuple[str, list[float]]] = []

    try:
        # sqlite-vec テーブルからエンベディングを取得
        placeholders = ",".join("?" * len(chunk_ids))
        rows = sqlite_db.conn.execute(
            f"SELECT chunk_id, embedding FROM memory_chunks_vec WHERE chunk_id IN ({placeholders})",
            chunk_ids,
        ).fetchall()

        for row in rows:
            chunk_id = str(row[0])
            raw_bytes = row[1]
            # sqlite-vec は float32 のバイナリ形式で格納。4の倍数でない場合は破損データとしてスキップ
            if isinstance(raw_bytes, bytes) and len(raw_bytes) > 0 and len(raw_bytes) % 4 == 0:
                n_floats = len(raw_bytes) // 4
                vec = list(struct.unpack(f"{n_floats}f", raw_bytes))
                embeddings.append((chunk_id, vec))

    except Exception as e:
        log.debug("sqlite-vec からの読み取りをスキップ: %s", e)
        return 0

    if not embeddings:
        return 0

    return pg_db.upsert_embeddings_batch(embeddings)
