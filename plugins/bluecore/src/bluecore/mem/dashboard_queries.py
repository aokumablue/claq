"""PostgreSQL ダッシュボード用集計クエリ"""

from __future__ import annotations

from typing import Any

from bluecore.mem.logger import get as _get_logger
from bluecore.mem.pg_database import PgDatabase

log = _get_logger("DASHBOARD")


def activity_by_user(pg: PgDatabase, days: int = 30) -> list[dict[str, Any]]:
    """ユーザー別アクティビティ（チャンク数）。

    Args:
        pg: PostgreSQL データベースハンドル。
        days: 集計対象とする直近の日数。

    Returns:
        ``user`` と ``chunks`` を持つ辞書のリスト（チャンク数降順）。
    """
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT origin_user, COUNT(*) AS chunk_count
           FROM memory_chunks
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
           GROUP BY origin_user
           ORDER BY chunk_count DESC""",
                (days,),
            )
            return [{"user": r[0], "chunks": r[1]} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)


def activity_by_project(pg: PgDatabase, days: int = 30) -> list[dict[str, Any]]:
    """プロジェクト別アクティビティ"""
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT project, COUNT(*) AS chunk_count
           FROM memory_chunks
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
           GROUP BY project
           ORDER BY chunk_count DESC""",
                (days,),
            )
            return [{"project": r[0], "chunks": r[1]} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)


def tool_usage_distribution(pg: PgDatabase, days: int = 30) -> list[dict[str, Any]]:
    """ツール使用分布"""
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT tool, COUNT(*) AS usage_count
           FROM memory_chunks,
                LATERAL jsonb_array_elements_text(tool_names::jsonb) AS tool
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
             AND tool_names IS NOT NULL AND tool_names != 'null'
           GROUP BY tool
           ORDER BY usage_count DESC
           LIMIT 20""",
                (days,),
            )
            return [{"tool": r[0], "count": r[1]} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)


def session_timeline(pg: PgDatabase, days: int = 30) -> list[dict[str, Any]]:
    """日次セッションタイムライン"""
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DATE(TO_TIMESTAMP(created_at_epoch)) AS day,
                  COUNT(DISTINCT session_id) AS sessions,
                  COUNT(*) AS chunks
           FROM memory_chunks
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
           GROUP BY day
           ORDER BY day""",
                (days,),
            )
            return [{"date": str(r[0]), "sessions": r[1], "chunks": r[2]} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)


def instinct_growth(pg: PgDatabase, days: int = 90) -> list[dict[str, Any]]:
    """インスティンクト成長推移"""
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT DATE(TO_TIMESTAMP(created_at_epoch)) AS day,
                  COUNT(*) AS new_instincts,
                  AVG(confidence) AS avg_confidence
           FROM instincts
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
           GROUP BY day
           ORDER BY day""",
                (days,),
            )
            return [{"date": str(r[0]), "count": r[1], "avg_confidence": round(float(r[2]), 2)} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)


def memory_quality_metrics(pg: PgDatabase) -> dict[str, Any]:
    """メモリ品質指標"""
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT
             COUNT(*) AS total_chunks,
             COUNT(*) FILTER (WHERE LENGTH(content) < 50) AS short_chunks,
             COUNT(*) FILTER (WHERE access_count > 0) AS accessed_chunks,
             AVG(access_count) AS avg_access_count,
             COUNT(DISTINCT session_id) AS total_sessions,
             COUNT(DISTINCT origin_user) AS total_users,
             COUNT(DISTINCT project) AS total_projects
           FROM memory_chunks"""
            )
            r = cur.fetchone()
            if r is None:
                return {}
            return {
                "total_chunks": r[0],
                "short_chunks": r[1],
                "short_chunk_rate": round(r[1] / max(r[0], 1) * 100, 1),
                "accessed_chunks": r[2],
                "access_rate": round(r[2] / max(r[0], 1) * 100, 1),
                "avg_access_count": round(float(r[3] or 0), 1),
                "total_sessions": r[4],
                "total_users": r[5],
                "total_projects": r[6],
            }
    finally:
        pg._put_conn(conn)


def file_change_heatmap(pg: PgDatabase, days: int = 30) -> list[dict[str, Any]]:
    """ファイル変更頻度ヒートマップ"""
    days = int(days)
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT file_path, COUNT(*) AS change_count
           FROM memory_chunks,
                LATERAL jsonb_array_elements_text(files_modified::jsonb) AS file_path
           WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - make_interval(days => %s))
             AND files_modified IS NOT NULL AND files_modified != 'null'
           GROUP BY file_path
           ORDER BY change_count DESC
           LIMIT 30""",
                (days,),
            )
            return [{"file": r[0], "changes": r[1]} for r in cur.fetchall()]
    finally:
        pg._put_conn(conn)
