"""PostgreSQL データを分析してスキル提案・改善提案を生成するモジュール"""

from __future__ import annotations

from typing import Any

from bluecore.mem.logger import get as _get_logger
from bluecore.mem.pg_database import PgDatabase

log = _get_logger("SKILL_ANALYZER")


_REPEATED_PATTERNS_SQL = """
        WITH tool_combos AS (
          SELECT
            session_id,
            project,
            origin_user,
            (
              SELECT STRING_AGG(t ORDER BY t, ',')
              FROM jsonb_array_elements_text(tool_names::jsonb) AS t
            ) AS tool_combo
          FROM memory_chunks
          WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
            AND tool_names IS NOT NULL AND tool_names != 'null' AND tool_names != '[]'
        )
        SELECT
          tool_combo,
          COUNT(*) AS occurrence_count,
          COUNT(DISTINCT project) AS project_count,
          COUNT(DISTINCT origin_user) AS user_count,
          ARRAY_AGG(DISTINCT project) AS projects,
          ARRAY_AGG(DISTINCT origin_user) AS users
        FROM tool_combos
        WHERE tool_combo IS NOT NULL AND tool_combo != ''
        GROUP BY tool_combo
        HAVING COUNT(*) >= %s
        ORDER BY occurrence_count DESC
        LIMIT 30
"""


def detect_repeated_patterns(
    pg: PgDatabase,
    min_count: int = 3,
    days: int = 90,
) -> list[dict[str, Any]]:
    """繰り返し実行されるパターン（ツール組み合わせ）を検出する。

    Args:
      pg: PgDatabase インスタンス
      min_count: 最低出現回数
      days: 集計期間（日）

    Returns:
      パターンリスト（tool_combo, count, projects, users 付き）
    """
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_REPEATED_PATTERNS_SQL, (days, min_count))
            rows = cur.fetchall()
            return [
                {
                    "tool_combo": r[0],
                    "tools": r[0].split(",") if r[0] else [],
                    "count": r[1],
                    "project_count": r[2],
                    "user_count": r[3],
                    "projects": list(r[4] or []),
                    "users": list(r[5] or []),
                }
                for r in rows
            ]
    finally:
        pg._put_conn(conn)


_SKILL_GAPS_SQL = """
        SELECT
          LOWER(REGEXP_REPLACE(
            SUBSTRING(user_prompt, 1, 60),
            '[^a-zA-Z0-9\\u3040-\\u9FFF\\s]', '', 'g'
          )) AS prompt_key,
          COUNT(*) AS occurrence_count,
          COUNT(DISTINCT origin_user) AS user_count,
          ARRAY_AGG(DISTINCT origin_user) AS users,
          MAX(user_prompt) AS sample_prompt
        FROM memory_chunks
        WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
          AND user_prompt IS NOT NULL AND user_prompt != ''
          AND LENGTH(user_prompt) > 10
        GROUP BY prompt_key
        HAVING COUNT(*) >= 2
        ORDER BY occurrence_count DESC
        LIMIT %s
"""


def detect_skill_gaps(
    pg: PgDatabase,
    days: int = 30,
    top_n: int = 20,
) -> list[dict[str, Any]]:
    """スキル化されていないが頻出するユーザープロンプトパターンを検出する。

    Args:
      pg: PgDatabase インスタンス
      days: 集計期間（日）
      top_n: 返却件数上限

    Returns:
      ギャップ候補リスト（prompt_prefix, count, users 付き）
    """
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(_SKILL_GAPS_SQL, (days, top_n))
            rows = cur.fetchall()
            return [
                {
                    "prompt_key": r[0],
                    "count": r[1],
                    "user_count": r[2],
                    "users": list(r[3] or []),
                    "sample_prompt": r[4],
                }
                for r in rows
            ]
    finally:
        pg._put_conn(conn)


_SKILL_LIKE_COND = (
    "LOWER(user_prompt) LIKE LOWER(%s) OR LOWER(content) LIKE LOWER(%s)"
)


def _fetch_skill_summary(
    pg: PgDatabase, skill_name: str, days: int
) -> tuple[Any, list[dict[str, Any]]]:
    """スキル集計行と日次タイムラインを返す。集計ゼロ時は (None, [])。"""
    like = f"%{skill_name}%"
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
        SELECT
          COUNT(*) AS total_uses,
          COUNT(DISTINCT origin_user) AS unique_users,
          COUNT(DISTINCT project) AS unique_projects,
          ARRAY_AGG(DISTINCT origin_user) AS users,
          ARRAY_AGG(DISTINCT project) AS projects,
          AVG(access_count) AS avg_access_count,
          MAX(created_at_epoch) AS last_used_epoch
        FROM memory_chunks
        WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
          AND ({_SKILL_LIKE_COND})
        """,
                (days, like, like),
            )
            r = cur.fetchone()
            if r is None or r[0] == 0:
                return None, []

            cur.execute(
                f"""
        SELECT DATE(TO_TIMESTAMP(created_at_epoch)) AS day, COUNT(*) AS uses
        FROM memory_chunks
        WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
          AND ({_SKILL_LIKE_COND})
        GROUP BY day ORDER BY day
        """,
                (days, like, like),
            )
            timeline = [{"date": str(tr[0]), "uses": tr[1]} for tr in cur.fetchall()]
            return r, timeline
    finally:
        pg._put_conn(conn)


def analyze_skill_usage(
    pg: PgDatabase,
    skill_name: str,
    days: int = 90,
) -> dict[str, Any]:
    """特定スキルの使用状況を分析する。

    スキル名がユーザープロンプトに含まれるチャンクを対象に集計する。

    Args:
      pg: PgDatabase インスタンス
      skill_name: 分析対象スキル名
      days: 集計期間（日）

    Returns:
      使用統計（total_uses, unique_users, projects, timeline 付き）
    """
    r, timeline = _fetch_skill_summary(pg, skill_name, days)
    if r is None:
        return {
            "total_uses": 0,
            "unique_users": 0,
            "unique_projects": 0,
            "users": [],
            "projects": [],
            "avg_access_count": 0.0,
            "last_used_epoch": None,
        }
    return {
        "total_uses": r[0],
        "unique_users": r[1],
        "unique_projects": r[2],
        "users": list(r[3] or []),
        "projects": list(r[4] or []),
        "avg_access_count": round(float(r[5] or 0), 2),
        "last_used_epoch": r[6],
        "timeline": timeline,
    }


def _query_top_tools(cur: Any, skill_name: str, days: int) -> list[dict[str, Any]]:
    """単一カーソルを使ってスキル使用セッションで頻出するツール上位10件を返す。"""
    like = f"%{skill_name}%"
    cur.execute(
        """
        WITH skill_sessions AS (
          SELECT DISTINCT session_id
          FROM memory_chunks
          WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
            AND (
              LOWER(user_prompt) LIKE LOWER(%s)
              OR LOWER(content) LIKE LOWER(%s)
            )
        ),
        session_tools AS (
          SELECT
            t AS tool_name,
            COUNT(*) AS usage_count
          FROM memory_chunks mc
          JOIN skill_sessions ss USING (session_id),
               LATERAL jsonb_array_elements_text(mc.tool_names::jsonb) AS t
          WHERE mc.tool_names IS NOT NULL AND mc.tool_names != 'null'
          GROUP BY t
          ORDER BY usage_count DESC
          LIMIT 10
        )
        SELECT tool_name, usage_count FROM session_tools
        """,
        (days, like, like),
    )
    return [{"tool": r[0], "count": r[1]} for r in cur.fetchall()]


def _query_top_files(cur: Any, skill_name: str, days: int) -> list[dict[str, Any]]:
    """単一カーソルを使ってスキル使用セッションで頻繁に変更されるファイル上位10件を返す。"""
    like = f"%{skill_name}%"
    cur.execute(
        """
        WITH skill_sessions AS (
          SELECT DISTINCT session_id
          FROM memory_chunks
          WHERE created_at_epoch > EXTRACT(EPOCH FROM NOW() - %s * INTERVAL '1 day')
            AND (
              LOWER(user_prompt) LIKE LOWER(%s)
              OR LOWER(content) LIKE LOWER(%s)
            )
        )
        SELECT
          file_path,
          COUNT(*) AS change_count
        FROM memory_chunks mc
        JOIN skill_sessions ss USING (session_id),
             LATERAL jsonb_array_elements_text(mc.files_modified::jsonb) AS file_path
        WHERE mc.files_modified IS NOT NULL AND mc.files_modified != 'null'
        GROUP BY file_path
        ORDER BY change_count DESC
        LIMIT 10
        """,
        (days, like, like),
    )
    return [{"file": r[0], "count": r[1]} for r in cur.fetchall()]


def _build_improvements(
    skill_name: str,
    top_tools: list[dict[str, Any]],
    top_files: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """ツール・ファイルパターンから改善提案リストを構築する。"""
    improvements: list[dict[str, Any]] = []

    if top_tools:
        high_usage_tools = [t for t in top_tools if t["count"] >= 3]
        if high_usage_tools:
            improvements.append({
                "type": "tool_coverage",
                "description": f"スキル '{skill_name}' のセッションで頻繁に使用されるツールを明示的に案内に追加することを検討してください",
                "evidence": high_usage_tools[:5],
                "priority": "medium",
            })

    if top_files:
        improvements.append({
            "type": "file_pattern",
            "description": f"スキル '{skill_name}' 使用時に頻繁に変更されるファイルパターンをドキュメントに追記することを検討してください",
            "evidence": top_files[:5],
            "priority": "low",
        })

    if not top_tools and not top_files:
        improvements.append({
            "type": "usage_low",
            "description": f"スキル '{skill_name}' の使用データが少なく十分な分析ができません。より多くのデータ蓄積が必要です",
            "evidence": [],
            "priority": "info",
        })

    return improvements


def suggest_skill_improvements(
    pg: PgDatabase,
    skill_name: str,
    days: int = 90,
) -> list[dict[str, Any]]:
    """スキルの改善提案を生成する。

    スキル使用後に続くツール操作パターンから、スキルがカバーしていない
    作業を検出して改善提案を生成する。

    Args:
      pg: PgDatabase インスタンス
      skill_name: 分析対象スキル名
      days: 集計期間（日）

    Returns:
      改善提案リスト（type, description, evidence 付き）
    """
    conn = pg._get_conn()
    try:
        with conn.cursor() as cur:
            top_tools = _query_top_tools(cur, skill_name, days)
            top_files = _query_top_files(cur, skill_name, days)
    finally:
        pg._put_conn(conn)
    return _build_improvements(skill_name, top_tools, top_files)
