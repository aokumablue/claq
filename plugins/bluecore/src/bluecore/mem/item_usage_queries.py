"""スキル・コマンド・エージェント使用率集計クエリ（SQLite/PostgreSQL共通）"""

from __future__ import annotations

import time
from typing import Any, Literal

from bluecore.mem.logger import get as _get_logger

log = _get_logger("ITEM_USAGE")

# SQLite は '?' 、PostgreSQL は '%s' をプレースホルダに使う
Placeholder = Literal["?", "%s"]
_SQLITE_PLACEHOLDER: Placeholder = "?"
_PG_PLACEHOLDER: Placeholder = "%s"



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


def align_team_counts(
    personal_labels: list[str],
    team_ranking: list[dict[str, Any]],
    item_type: str,
) -> list[int]:
    """チームランキングを個人ラベル順にアライメントする。

    Args:
        personal_labels: 個人ランキングのラベル一覧（順序の基準）
        team_ranking: item_usage_ranking() のチームデータ
        item_type: "skill" | "command" | "agent"

    Returns:
        personal_labels と同順のチーム使用回数リスト。未使用は 0。
    """
    team_map = {r["item_name"]: r["uses"] for r in team_ranking if r["item_type"] == item_type}
    return [team_map.get(name, 0) for name in personal_labels]


def item_usage_ranking(
    conn: Any,
    placeholder: Placeholder = _SQLITE_PLACEHOLDER,
    days: int = 30,
) -> list[dict[str, Any]]:
    """item_type 別使用回数ランキングを返す。

    Args:
        conn: sqlite3.Connection または psycopg.Connection
        placeholder: SQL プレースホルダ（'?' or '%s'）
        days: 集計期間（日数）

    Returns:
        [{"item_name": str, "item_type": str, "uses": int, "last_used_epoch": int | None}]
        uses の降順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    # プレースホルダをリテラル直書きした完成SQLをDB種別ごとに用意する（CWE-89対策）
    if placeholder == _PG_PLACEHOLDER:
        sql = """
            SELECT skill_name, item_type, COUNT(*) AS uses,
                   MAX(created_at_epoch) AS last_used_epoch
            FROM mem_item_runs
            WHERE created_at_epoch > %s
            GROUP BY skill_name, item_type
            ORDER BY uses DESC
        """
    else:
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


_DAILY_TREND_SQL_PG = """
    SELECT DATE(TO_TIMESTAMP(created_at_epoch)) AS day,
           SUM(CASE WHEN item_type = 'skill' THEN 1 ELSE 0 END) AS skill_count,
           SUM(CASE WHEN item_type = 'command' THEN 1 ELSE 0 END) AS command_count,
           SUM(CASE WHEN item_type = 'agent' THEN 1 ELSE 0 END) AS agent_count,
           COUNT(*) AS total
    FROM mem_item_runs
    WHERE created_at_epoch > %s
    GROUP BY day
    ORDER BY day
"""

_DAILY_TREND_SQL_SQLITE = """
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


def daily_trend(
    conn: Any,
    placeholder: Placeholder = _SQLITE_PLACEHOLDER,
    days: int = 30,
) -> list[dict[str, Any]]:
    """日次実行数推移を返す（item_type 別に集計）。

    Args:
        conn: sqlite3.Connection または psycopg.Connection
        placeholder: SQL プレースホルダ
        days: 集計期間（日数）

    Returns:
        [{"date": str, "skill": int, "command": int, "agent": int, "total": int}]
        date の昇順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    sql = _DAILY_TREND_SQL_PG if placeholder == _PG_PLACEHOLDER else _DAILY_TREND_SQL_SQLITE
    rows = _execute(conn, sql, (since_epoch,))
    return [
        {"date": str(r[0]), "skill": r[1], "command": r[2], "agent": r[3], "total": r[4]}
        for r in rows
    ]


def outcome_distribution(
    conn: Any,
    placeholder: Placeholder = _SQLITE_PLACEHOLDER,
    days: int = 30,
) -> list[dict[str, Any]]:
    """アウトカム分布（success/partial/failure/unknown）を返す。

    Args:
        conn: sqlite3.Connection または psycopg.Connection
        placeholder: SQL プレースホルダ
        days: 集計期間（日数）

    Returns:
        [{"outcome": str, "count": int}] count の降順でソート済み
    """
    since_epoch = int(time.time()) - days * 86400
    # プレースホルダをリテラル直書きした完成SQLをDB種別ごとに用意する（CWE-89対策）
    if placeholder == _PG_PLACEHOLDER:
        sql = """
            SELECT outcome, COUNT(*) AS cnt
            FROM mem_item_runs
            WHERE created_at_epoch > %s
            GROUP BY outcome
            ORDER BY cnt DESC
        """
    else:
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
    """SQLite / psycopg 両対応のクエリ実行ヘルパー。"""
    # モジュール名で判定: サイドエフェクトなく確実に DB 種別を識別できる
    if type(conn).__module__.startswith("psycopg"):
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    # sqlite3 は直接 execute
    return conn.execute(sql, params).fetchall()
