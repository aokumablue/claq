"""mem CLI: sync handlers."""

from __future__ import annotations

import json
import sys
from typing import Any, TypedDict
from urllib.parse import urlparse, urlunparse

from bluecore.mem.logger import get as _get_logger

log = _get_logger("SYNC_HANDLERS")


class SyncStatusDict(TypedDict):
    """handle_sync_status / _build_sync_status_dict の出力型。"""

    enabled: bool
    postgres_url_set: bool
    postgres_url_masked: str | None
    psycopg_installed: bool
    connection: str
    connection_error: str | None
    pending_rows: int | None
    last_sync_success: bool
    last_sync_at: float | None


def handle_sync(settings, stdin_data: dict[str, Any]) -> None:
    """PostgreSQL への同期を実行する。"""
    from bluecore.mem.sync import sync_to_postgres

    dry_run = stdin_data.get("dry_run", False)
    result = sync_to_postgres(settings, dry_run=dry_run)

    output = {
        "success": result.success,
        "error": result.error,
        "synced": {
            "chunks": result.chunks,
            "sessions": result.sessions,
            "instincts": result.instincts,
            "adrs": result.adrs,
            "events": result.events,
            "embeddings": result.embeddings,
            "interaction_logs": result.interaction_logs,
            "project_profiles": result.project_profiles,
            "skill_runs": result.skill_runs,
            "session_digests": result.session_digests,
        },
    }
    print(json.dumps(output, ensure_ascii=False))


def handle_sync_check(settings, *, log: Any) -> None:
    """同期間隔をチェックし、必要なら同期を実行する。"""
    from bluecore.mem.sync import should_sync, sync_to_postgres

    if not should_sync(settings):
        log.info("sync-check: スキップ")
        return

    log.info("sync-check: 同期実行")
    result = sync_to_postgres(settings)

    if not result.success:
        log.error("sync-check: 同期失敗 - %s", result.error)
        # async:true のフックでも ~/.bluecore/logs/ 経由で原因が確認できるよう stderr にも出す
        print(f"[sync-check] 同期失敗: {result.error}", file=sys.stderr)


def handle_sync_status(settings, stdin_data: dict[str, Any]) -> None:  # noqa: ARG001
    """同期設定・接続状態・pending 件数を JSON で報告する。

    処理継続を保証するため、各フィールドが取得不可な場合は null を入れて構造を維持する。
    """
    output = _build_sync_status_dict(settings, lite=False)
    print(json.dumps(output, ensure_ascii=False))


def _build_sync_status_dict(settings, *, lite: bool = False) -> SyncStatusDict:
    """sync 設定・接続状態・pending 件数を dict で返す。

    lite=True の場合は接続テストを省略し connection="skipped" を返す（セッション開始時の軽量診断用）。
    """
    from bluecore.mem.sync import _mask_url

    sync_cfg = settings.sync
    postgres_url_set = bool(sync_cfg.postgres_url)
    masked_url = _mask_url(sync_cfg.postgres_url) if postgres_url_set else None

    psycopg_installed = _check_psycopg()

    if lite:
        connection_result: str = "skipped"
        connection_error: str | None = None
        pending_rows: int | None = None
    else:
        connection_result, connection_error = _test_pg_connection(sync_cfg.postgres_url, psycopg_installed)
        pending_rows = _count_all_pending(settings) if connection_result == "ok" else None

    return SyncStatusDict(
        enabled=sync_cfg.enabled,
        postgres_url_set=postgres_url_set,
        postgres_url_masked=masked_url,
        psycopg_installed=psycopg_installed,
        connection=connection_result,
        connection_error=connection_error,
        pending_rows=pending_rows,
        last_sync_success=sync_cfg.last_sync_success,
        last_sync_at=sync_cfg.last_synced_at or None,
    )


def _check_psycopg() -> bool:
    """psycopg がインストール済みか確認する。"""
    try:
        import psycopg  # noqa: F401
        return True
    except ImportError:
        return False


def _test_pg_connection(postgres_url: str, psycopg_installed: bool) -> tuple[str, str | None]:
    """PG 接続を試みて (status, error_message) を返す。"""
    if not postgres_url:
        return "skipped", None
    if not psycopg_installed:
        return "skipped", "psycopg が未インストールです"

    from bluecore.mem.pg_database import PgDatabase

    pg_db = PgDatabase(postgres_url, use_pool=False)
    try:
        ok = pg_db.test_connection()
        return ("ok", None) if ok else ("failed", "接続テスト失敗")
    except Exception as e:
        log.debug("PG接続テスト例外: %s", e)
        return "failed", "接続に失敗しました（詳細はサーバーログを確認してください）"
    finally:
        pg_db.close()


def _split_password(url: str) -> tuple[str, str | None]:
    """URL からパスワードを分離する。

    Returns:
        (password_stripped_url, password_or_None)
    """
    parsed = urlparse(url)
    if not parsed.password:
        return url, None
    password = parsed.password
    # netloc からパスワードを除去
    userinfo = parsed.username or ""
    host_part = parsed.hostname or ""
    if parsed.port:
        host_part = f"{host_part}:{parsed.port}"
    new_netloc = f"{userinfo}@{host_part}" if userinfo else host_part
    new_parsed = parsed._replace(netloc=new_netloc)
    return urlunparse(new_parsed), password


def _write_pgpass(host: str, port: int | str, db: str, user: str, password: str) -> None:
    """<data_dir>/.pgpass にエントリを追加する（重複は追加しない）。"""
    from bluecore.mem.settings import _write_pgpass_entry

    _write_pgpass_entry(host, port, db, user, password)


def _count_all_pending(settings) -> int:
    """全テーブルの未同期行数合計を返す。"""
    from bluecore.mem.database import Database
    from bluecore.mem.sync import _SYNC_TABLES, _count_pending_rows

    try:
        db = Database(settings.db_path)
        try:
            return sum(_count_pending_rows(db.conn, t) for t in _SYNC_TABLES)
        finally:
            db.close()
    except Exception as e:
        log.error("pending 件数取得失敗: %s", e)
        return 0
