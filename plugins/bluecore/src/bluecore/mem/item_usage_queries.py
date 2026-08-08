"""SQLite のスキル・コマンド・エージェント使用率集計クエリ"""

from __future__ import annotations

import time
from typing import Any

from bluecore.mem.logger import get as _get_logger

log = _get_logger("ITEM_USAGE")


def make_ranking_data(ranking: list[dict[str, Any]], item_type: str) -> tuple[list[str], list[int]]:
    """指定 item_type のランキングデータを (labels, counts) に変換する。

    Args:
        ranking: item_usage_ranking() の戻り値
        item_type: "skill" | "command" | "agent"

    Returns:
        (labels, counts) のタプル。uses の降順は呼び出し元で保証済み。
    """
    filtered = [r for r in ranking if r["item_type"] == item_type]
    return [r["item_name"] for r in filtered], [r["uses"] for r in filtered]


def item_usage_ranking(conn: Any, days: int = 30) -> list[dict[str, Any]]:
    """item_type 別使用回数ランキングを返す。

    Args:
        conn: sqlite3.Connection
        days: 集計期間（日数）

    Returns:
        [{"item_name": str, "item_type": str, "uses": int, "last_used_epoch": int | None}]
        uses の降順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    sql = """
        SELECT skill_name, item_type, COUNT(*) AS uses,
               MAX(created_at_epoch) AS last_used_epoch
        FROM mem_item_runs
        WHERE created_at_epoch > ?
        GROUP BY skill_name, item_type
        ORDER BY uses DESC
    """
    rows = _execute(conn, sql, (since_epoch,))
    return [
        {
            "item_name": r[0],
            "item_type": r[1],
            "uses": r[2],
            "last_used_epoch": r[3],
        }
        for r in rows
    ]


_DAILY_TREND_SQL = """
    SELECT date(created_at_epoch, 'unixepoch') AS day,
           SUM(CASE WHEN item_type = 'skill' THEN 1 ELSE 0 END) AS skill_count,
           SUM(CASE WHEN item_type = 'command' THEN 1 ELSE 0 END) AS command_count,
           SUM(CASE WHEN item_type = 'agent' THEN 1 ELSE 0 END) AS agent_count,
           COUNT(*) AS total
    FROM mem_item_runs
    WHERE created_at_epoch > ?
    GROUP BY day
    ORDER BY day
"""


def daily_trend(conn: Any, days: int = 30) -> list[dict[str, Any]]:
    """日次実行数推移を返す（item_type 別に集計）。

    Args:
        conn: sqlite3.Connection
        days: 集計期間（日数）

    Returns:
        [{"date": str, "skill": int, "command": int, "agent": int, "total": int}]
        date の昇順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    rows = _execute(conn, _DAILY_TREND_SQL, (since_epoch,))
    return [
        {"date": str(r[0]), "skill": r[1], "command": r[2], "agent": r[3], "total": r[4]}
        for r in rows
    ]


def outcome_distribution(conn: Any, days: int = 30) -> list[dict[str, Any]]:
    """アウトカム分布（success/partial/failure/unknown）を返す。

    Args:
        conn: sqlite3.Connection
        days: 集計期間（日数）

    Returns:
        [{"outcome": str, "count": int}] count の降順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    sql = """
        SELECT outcome, COUNT(*) AS cnt
        FROM mem_item_runs
        WHERE created_at_epoch > ?
        GROUP BY outcome
        ORDER BY cnt DESC
    """
    rows = _execute(conn, sql, (since_epoch,))
    return [{"outcome": r[0], "count": r[1]} for r in rows]


def _execute(conn: Any, sql: str, params: tuple) -> list[Any]:
    """SQLite クエリを実行する。"""
    return conn.execute(sql, params).fetchall()
