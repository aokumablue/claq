"""SQLite → PostgreSQL 同期ロジック"""

from __future__ import annotations

import errno
import fcntl
import re
import sqlite3
import struct
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from bluecore.lib.constants import BASE_DIR_NAME
from bluecore.lib.core_utils import get_git_user_name
from bluecore.mem.database import (
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
from bluecore.mem.logger import get as _get_logger
from bluecore.mem.pg_database import PgDatabase
from bluecore.mem.settings import Settings

log = _get_logger("SYNC")


# 文中に埋め込まれた接続 URL（例外メッセージ等）の user:password@ を検出する。
# パスワード部は貪欲マッチ + バックトラックで @ 含みパスワードにも対応する。
_EMBEDDED_URL_PASSWORD_RE = re.compile(r"(://[^/\s:@]+):([^\s/]+)@")


def _mask_url(url: str) -> str:
    """接続 URL のパスワード部を *** に置換する。@ 含みパスワードと文中埋め込み URL に対応する。"""
    try:
        parsed = urlparse(url)
        if parsed.password is not None:
            netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@", 1)
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:
        pass
    return _EMBEDDED_URL_PASSWORD_RE.sub(r"\1:***@", url)


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

@dataclass(frozen=True)
class ClaimConfig:
    """_claim_pending_rows の設定（テーブル・ソート列・同期タイムスタンプ・行ファクトリ）。"""

    table: str
    order_by: str
    synced_at: str
    row_factory: Callable[..., object]
    batch_size: int = _SYNC_BATCH_SIZE


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


def _claim_pending_rows[T](conn: sqlite3.Connection, cfg: ClaimConfig) -> list[T]:
    """未同期行を取得して synced_at を立てる（最大 batch_size 件）。"""
    if cfg.table not in _SYNC_TABLES:
        raise ValueError(f"Invalid table: {cfg.table}")
    if cfg.order_by not in _ALLOWED_ORDER_BY_COLUMNS:
        raise ValueError(f"Invalid order_by column: {cfg.order_by}")
    rows = conn.execute(
        f"SELECT * FROM {cfg.table} WHERE synced_at IS NULL ORDER BY {cfg.order_by} LIMIT ?",
        (cfg.batch_size,),
    ).fetchall()
    if not rows:
        return []

    conn.executemany(
        f"UPDATE {cfg.table} SET synced_at = ? WHERE id = ?",
        [(cfg.synced_at, row["id"]) for row in rows],
    )
    return [cfg.row_factory(row) for row in rows]


def _count_pending_embeddings(conn: sqlite3.Connection, chunk_ids: list[str]) -> int:
    """未同期チャンクに紐づく埋め込み件数を数える。"""
    if not chunk_ids:
        return 0

    # chunk_ids は _SYNC_BATCH_SIZE 以下に制限されるためプレースホルダ数は安全
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
        return Path.home() / BASE_DIR_NAME / "sync.lock"


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
    """PostgreSQL 同期の結果。各フィールドは同期（UPSERT/INSERT）した件数を表す。

    ``embeddings`` は memory_chunks_vec テーブルへの同期件数。
    ``skill_runs`` は mem_item_runs テーブルへの同期件数。
    ``success=False`` かつ ``error`` に理由が入る場合は同期が中断されたことを示す。
    """

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


def _dry_run_counts(sqlite_db: Database) -> SyncResult:
    """DRY RUN 用に未同期行数を数えて SyncResult を返す。"""
    conn = sqlite_db.conn
    result = SyncResult(
        chunks=_count_pending_rows(conn, "memory_chunks"),
        sessions=_count_pending_rows(conn, "sessions"),
        instincts=_count_pending_rows(conn, "instincts"),
        adrs=_count_pending_rows(conn, "adrs"),
        events=_count_pending_rows(conn, "event_logs"),
        interaction_logs=_count_pending_rows(conn, "interaction_logs"),
        project_profiles=_count_pending_rows(conn, "project_profiles"),
        skill_runs=_count_pending_rows(conn, "mem_item_runs"),
    )
    chunk_ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM memory_chunks WHERE synced_at IS NULL ORDER BY created_at_epoch"
        ).fetchall()
    ]
    result.embeddings = _count_pending_embeddings(conn, chunk_ids)
    log.info(
        "[DRY RUN] 同期対象: chunks=%d, sessions=%d, instincts=%d, adrs=%d, "
        "events=%d, interactions=%d, profiles=%d, skill_runs=%d",
        result.chunks, result.sessions, result.instincts, result.adrs,
        result.events, result.interaction_logs, result.project_profiles, result.skill_runs,
    )
    return result


def _claim_all_pending(conn: sqlite3.Connection, sync_started_at: str) -> dict:
    """全テーブルの未同期行を一括で取得して synced_at を立てる。

    Returns:
        テーブル名をキー、dataclass インスタンスのリストを値とする dict。
        keys: chunks / sessions / instincts / adrs / events /
              interaction_logs / project_profiles / skill_runs。
    """
    def _cfg(table: str, order_by: str, row_factory: Callable[..., object]) -> ClaimConfig:
        return ClaimConfig(table=table, order_by=order_by, synced_at=sync_started_at, row_factory=row_factory)

    return {
        "chunks": _claim_pending_rows(conn, _cfg("memory_chunks", "created_at_epoch", _row_to_chunk)),
        "sessions": _claim_pending_rows(conn, _cfg("sessions", "started_at_epoch", _row_to_session)),
        "instincts": _claim_pending_rows(conn, _cfg("instincts", "created_at_epoch", _row_to_instinct)),
        "adrs": _claim_pending_rows(conn, _cfg("adrs", "created_at_epoch", _row_to_adr)),
        "events": _claim_pending_rows(conn, _cfg("event_logs", "created_at_epoch", _row_to_event_log)),
        "interaction_logs": _claim_pending_rows(conn, _cfg("interaction_logs", "created_at_epoch", _row_to_interaction_log)),
        "project_profiles": _claim_pending_rows(conn, _cfg("project_profiles", "last_updated_epoch", _row_to_project_profile)),
        "skill_runs": _claim_pending_rows(conn, _cfg("mem_item_runs", "created_at_epoch", _row_to_mem_item_run)),
    }


def _upsert_with_origin(items: list, origin_user: str) -> list:
    """origin_user を上書きした新しいインスタンスのリストを返す（元オブジェクト不変）。

    Args:
        items: dataclass インスタンスのリスト。
        origin_user: 上書きする origin_user 値。

    Returns:
        origin_user が置き換えられた新しいインスタンスのリスト。
    """
    import dataclasses
    return [dataclasses.replace(item, origin_user=origin_user) for item in items]


def _upsert_all_to_pg(pg_db: PgDatabase, pending: dict, origin_user: str) -> SyncResult:
    """pending 辞書の各テーブルデータを PostgreSQL に UPSERT して SyncResult を返す。

    Args:
        pg_db: 接続済みの PgDatabase インスタンス。
        pending: ``_claim_all_pending`` が返した dict（テーブル名→dataclassリスト）。
        origin_user: UPSERT 時に origin_user フィールドへ設定するユーザー識別子。

    Returns:
        各テーブルの同期件数を持つ SyncResult（embeddings は呼び出し側で設定）。
    """
    chunks = pending["chunks"]
    result = SyncResult(chunks=pg_db.upsert_chunks_batch(chunks, origin_user)) if chunks else SyncResult()
    if chunks:
        log.info("chunks: %d 件同期", result.chunks)

    if sessions := pending["sessions"]:
        result.sessions = pg_db.upsert_sessions_batch(sessions, origin_user)
        log.info("sessions: %d 件同期", result.sessions)

    if instincts := _upsert_with_origin(pending["instincts"], origin_user):
        result.instincts = pg_db.upsert_instincts_batch(instincts)
        log.info("instincts: %d 件同期", result.instincts)

    if adrs := _upsert_with_origin(pending["adrs"], origin_user):
        result.adrs = pg_db.upsert_adrs_batch(adrs)
        log.info("adrs: %d 件同期", result.adrs)

    if events := _upsert_with_origin(pending["events"], origin_user):
        result.events = pg_db.insert_event_logs_batch(events)
        log.info("events: %d 件同期", result.events)

    if interaction_logs := _upsert_with_origin(pending["interaction_logs"], origin_user):
        result.interaction_logs = pg_db.upsert_interaction_logs_batch(interaction_logs)
        log.info("interaction_logs: %d 件同期", result.interaction_logs)

    if project_profiles := _upsert_with_origin(pending["project_profiles"], origin_user):
        result.project_profiles = pg_db.upsert_project_profiles_batch(project_profiles)
        log.info("project_profiles: %d 件同期", result.project_profiles)

    if skill_runs := _upsert_with_origin(pending["skill_runs"], origin_user):
        result.skill_runs = pg_db.upsert_mem_item_runs_batch(skill_runs)
        log.info("skill_runs: %d 件同期", result.skill_runs)

    return result


def _run_sync_transaction(
    sqlite_db: Database,
    pg_db: PgDatabase,
    origin_user: str,
) -> SyncResult:
    """SQLite トランザクション内でデータを取得し PG へ UPSERT する。"""
    sync_started_at = datetime.now(UTC).isoformat()
    with sqlite_db.begin_immediate_transaction() as conn:
        pending = _claim_all_pending(conn, sync_started_at)
        result = _upsert_all_to_pg(pg_db, pending, origin_user)
        result.embeddings = _sync_embeddings(sqlite_db, pg_db, pending["chunks"])
        if result.embeddings > 0:
            log.info("embeddings: %d 件同期", result.embeddings)
    return result


def _check_sync_preconditions(settings: Settings) -> SyncResult | None:
    """同期の事前条件を検証し、スキップすべき場合は SyncResult を返す。問題なければ None。"""
    sync_cfg = settings.sync
    if not sync_cfg.enabled:
        log.info("同期は無効です")
        return SyncResult(success=True)
    if not sync_cfg.postgres_url:
        log.info("同期スキップ: postgres_url 未設定 (~/.bluecore/settings.json の mem.sync.postgres_url を設定してください)")
        return SyncResult(success=False, error="postgres_url が設定されていません")
    if not should_sync(settings):
        log.info("同期は最新状態のためスキップします")
        return SyncResult(success=True)
    return None


def _open_sync_connections(
    settings: Settings,
) -> tuple[Database, PgDatabase] | tuple[None, None]:
    """SQLite + PgDatabase を開いて返す。PG 接続失敗時は (None, None)。"""
    sync_cfg = settings.sync
    sqlite_db = Database(settings.db_path)
    pg_db = PgDatabase(sync_cfg.postgres_url)
    if not pg_db.test_connection():
        sync_cfg.last_sync_success = False
        try:
            settings.save_sync_state()
        except Exception:
            pass
        log.error("PG 接続失敗: %s", _mask_url(sync_cfg.postgres_url))
        sqlite_db.close()
        pg_db.close()
        return None, None
    return sqlite_db, pg_db


def _close_sync_connections(
    sqlite_db: Database | None, pg_db: PgDatabase | None
) -> None:
    """同期用の DB 接続をクローズする。"""
    if sqlite_db is not None:
        sqlite_db.close()
    if pg_db is not None:
        pg_db.close()


def _execute_sync(
    settings: Settings,
    sqlite_db: Database,
    pg_db: PgDatabase,
    origin_user: str,
    dry_run: bool,
) -> SyncResult:
    """実際の同期処理を実行し、成功・失敗フラグを保存して返す。"""
    sync_cfg = settings.sync
    if dry_run:
        log.info("[DRY RUN] 同期をシミュレート中...")
        return _dry_run_counts(sqlite_db)

    log.info("PostgreSQL への同期を開始...")
    result = _run_sync_transaction(sqlite_db, pg_db, origin_user)
    sync_cfg.last_synced_at = time.time()
    sync_cfg.last_sync_success = True
    settings.save_sync_state()
    log.info("同期完了")
    return result


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
        early = _check_sync_preconditions(settings)
        if early is not None:
            return early

        settings.sync.last_sync_attempt_at = time.time()
        origin_user = get_git_user_name()
        sqlite_db: Database | None = None
        pg_db: PgDatabase | None = None
        try:
            sqlite_db, pg_db = _open_sync_connections(settings)
            if sqlite_db is None or pg_db is None:
                return SyncResult(success=False, error="PostgreSQL への接続に失敗しました")
            return _execute_sync(settings, sqlite_db, pg_db, origin_user, dry_run)
        except Exception as e:
            settings.sync.last_sync_success = False
            try:
                settings.save_sync_state()
            except Exception:
                pass
            # traceback は DB ドライバのスタックに接続情報が混入し得るため出さず、メッセージはマスクする
            log.error("同期エラー: %s", _mask_url(str(e)))
            return SyncResult(success=False, error=_mask_url(str(e)))
        finally:
            _close_sync_connections(sqlite_db, pg_db)


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

    Raises:
        Exception: vec テーブル不在以外の読み取り失敗・PG UPSERT 失敗時。
            呼び出し元トランザクションの rollback で synced_at が立たず再同期される。
    """
    if not chunks:
        return 0

    chunk_ids = [str(c.id) for c in chunks]
    embeddings: list[tuple[str, list[float]]] = []

    try:
        # sqlite-vec テーブルからエンベディングを取得（chunk_ids は _SYNC_BATCH_SIZE 以下）
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

    except sqlite3.OperationalError as e:
        # sqlite-vec 拡張なし環境では vec テーブルが存在しない（正常系スキップ）。
        # それ以外の読み取り失敗は伝播させ、トランザクション rollback で再同期可能にする。
        if "no such table" in str(e):
            log.debug("sqlite-vec からの読み取りをスキップ: %s", e)
            return 0
        raise

    if not embeddings:
        return 0

    return pg_db.upsert_embeddings_batch(embeddings)
