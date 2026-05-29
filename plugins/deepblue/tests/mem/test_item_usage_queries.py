"""item_usage_queries モジュールのテスト"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from devgear.mem.database import Database, MemItemRun
from devgear.mem.item_usage_queries import (
    _SQLITE_PLACEHOLDER,
    align_team_counts,
    daily_trend,
    item_usage_ranking,
    make_ranking_data,
    outcome_distribution,
)

# --- フィクスチャ ---


@pytest.fixture
def db_conn():
    """一時 SQLite データベースの接続を返す。"""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test.db"
        db = Database(db_path)
        yield db.conn
        db.close()


@pytest.fixture
def db_with_records(db_conn):
    """サンプルアイテム実行記録を挿入した接続を返す。"""
    now = int(time.time())
    records = [
        ("s-learn", "skill", "success", now - 100),
        ("s-learn", "skill", "success", now - 200),
        ("s-learn", "skill", "failure", now - 300),
        ("s-tdd", "skill", "success", now - 150),
        ("c-dashboard", "command", "success", now - 50),
        ("c-dashboard", "command", "unknown", now - 400),
        ("a-review", "agent", "success", now - 80),
    ]
    for skill_name, item_type, outcome, epoch in records:
        db_conn.execute(
            """INSERT INTO mem_item_runs
            (id, origin_user, session_id, project,
             skill_name, item_type, outcome, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (f"id-{skill_name}-{epoch}", "", "sess-1", "proj",
              skill_name, item_type, outcome, epoch),
        )
    db_conn.commit()
    return db_conn


# --- item_usage_ranking ---


class TestItemUsageRanking:
    def test_returns_all_records(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        assert len(result) == 4  # s-learn, s-tdd, c-dashboard, a-review

    def test_sorted_by_uses_desc(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        uses = [r["uses"] for r in result]
        assert uses == sorted(uses, reverse=True)

    def test_correct_item_type(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        type_map = {r["item_name"]: r["item_type"] for r in result}
        assert type_map["s-learn"] == "skill"
        assert type_map["c-dashboard"] == "command"
        assert type_map["a-review"] == "agent"

    def s_learn_uses_count(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        skill = next(r for r in result if r["item_name"] == "s-learn")
        assert skill["uses"] == 3

    def test_excludes_old_records(self, db_conn: sqlite3.Connection) -> None:
        old_epoch = int(time.time()) - 40 * 86400  # 40日前
        db_conn.execute(
            """INSERT INTO mem_item_runs
            (id, origin_user, session_id, project,
             skill_name, item_type, outcome, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("old-id", "", "sess", "proj", "s-old", "skill", "success", old_epoch),
        )
        db_conn.commit()
        result = item_usage_ranking(db_conn, _SQLITE_PLACEHOLDER, days=30)
        names = [r["item_name"] for r in result]
        assert "s-old" not in names

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_conn, _SQLITE_PLACEHOLDER, days=30)
        assert result == []


# --- daily_trend ---


class TestDailyTrend:
    def test_returns_list(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        assert isinstance(result, list)

    def test_has_required_keys(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        if result:
            row = result[0]
            assert set(row.keys()) == {"date", "skill", "command", "agent", "total"}

    def test_total_equals_sum(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        for row in result:
            assert row["total"] == row["skill"] + row["command"] + row["agent"]

    def test_dates_sorted(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = daily_trend(db_conn, _SQLITE_PLACEHOLDER, days=30)
        assert result == []

    def test_postgres_placeholder_uses_psycopg_execution(self) -> None:
        class _Cursor:
            def __init__(self) -> None:
                self.executed: list[tuple[str, tuple]] = []

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
                return False

            def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
                self.executed.append((sql, params))

            def fetchall(self):  # noqa: ANN001
                return [("2024-01-01", 1, 2, 3, 6)]

        class _Conn:
            __module__ = "psycopg.connection"

            def __init__(self) -> None:
                self.cursor_obj = _Cursor()

            def cursor(self):
                return self.cursor_obj

        conn = _Conn()
        result = daily_trend(conn, "%s", days=1)
        assert result == [{"date": "2024-01-01", "skill": 1, "command": 2, "agent": 3, "total": 6}]
        assert "TO_TIMESTAMP" in conn.cursor_obj.executed[0][0]

        out = outcome_distribution(conn, "%s", days=1)
        assert out == [{"outcome": "2024-01-01", "count": 1}]


# --- outcome_distribution ---


class TestOutcomeDistribution:
    def test_returns_all_outcomes(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        outcomes = {r["outcome"] for r in result}
        assert outcomes == {"success", "failure", "unknown"}

    def test_success_count(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        success = next(r for r in result if r["outcome"] == "success")
        assert success["count"] == 5  # 7件中 success が5件

    def test_sorted_by_count_desc(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, _SQLITE_PLACEHOLDER, days=365)
        counts = [r["count"] for r in result]
        assert counts == sorted(counts, reverse=True)

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = outcome_distribution(db_conn, _SQLITE_PLACEHOLDER, days=30)
        assert result == []


# --- DB マイグレーション: item_type 列の確認 ---


class TestItemTypeColumn:
    def test_mem_item_runs_has_item_type_column(self, db_conn: sqlite3.Connection) -> None:
        """mem_item_runs テーブルに item_type 列が存在することを確認する。"""
        cols = {row["name"] for row in db_conn.execute("PRAGMA table_info(mem_item_runs)").fetchall()}
        assert "item_type" in cols

    def test_default_item_type_is_skill(self, db_conn: sqlite3.Connection) -> None:
        """item_type のデフォルト値が 'skill' であることを確認する。"""
        now = int(time.time())
        db_conn.execute(
            """INSERT INTO mem_item_runs
            (id, origin_user, session_id, project,
             skill_name, outcome, created_at_epoch)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("test-default", "", "sess", "proj", "s-test", "success", now),
        )
        db_conn.commit()
        row = db_conn.execute(
            "SELECT item_type FROM mem_item_runs WHERE id = ?", ("test-default",)
        ).fetchone()
        assert row["item_type"] == "skill"

    def test_store_mem_item_run_with_item_type(self, tmp_path: Path) -> None:
        """Database.store_mem_item_run() で item_type を指定して保存できることを確認する。"""
        db = Database(tmp_path / "test.db")
        run = MemItemRun(
            session_id="sess-1",
            project="proj",
            skill_name="c-dashboard",
            created_at_epoch=int(time.time()),
            item_type="command",
        )
        run_id = db.store_mem_item_run(run)
        assert run_id
        row = db.conn.execute(
            "SELECT item_type FROM mem_item_runs WHERE id = ?", (run_id,)
        ).fetchone()
        assert row["item_type"] == "command"
        db.close()


# --- make_ranking_data / align_team_counts ---


class TestMakeRankingData:
    """make_ranking_data のユニットテスト。"""

    RANKING = [
        {"item_name": "s-learn", "item_type": "skill", "uses": 5, "last_used_epoch": None},
        {"item_name": "s-tdd", "item_type": "skill", "uses": 2, "last_used_epoch": None},
        {"item_name": "c-dash", "item_type": "command", "uses": 3, "last_used_epoch": None},
        {"item_name": "a-review", "item_type": "agent", "uses": 1, "last_used_epoch": None},
    ]

    def test_skill_labels_and_counts(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "skill")
        assert labels == ["s-learn", "s-tdd"]
        assert counts == [5, 2]

    def test_command_labels_and_counts(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "command")
        assert labels == ["c-dash"]
        assert counts == [3]

    def test_empty_type(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "agent")
        assert labels == ["a-review"]
        assert counts == [1]

    def test_no_match_returns_empty(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "unknown_type")
        assert labels == []
        assert counts == []


class TestAlignTeamCounts:
    """align_team_counts のユニットテスト。"""

    PERSONAL_LABELS = ["s-learn", "s-tdd", "s-new"]
    TEAM_RANKING = [
        {"item_name": "s-learn", "item_type": "skill", "uses": 10, "last_used_epoch": None},
        {"item_name": "s-other", "item_type": "skill", "uses": 7, "last_used_epoch": None},
    ]

    def test_aligns_to_personal_order(self) -> None:
        result = align_team_counts(self.PERSONAL_LABELS, self.TEAM_RANKING, "skill")
        # s-learn=10, s-tdd=未使用=0, s-new=未使用=0
        assert result == [10, 0, 0]

    def test_missing_items_get_zero(self) -> None:
        result = align_team_counts(["s-tdd"], self.TEAM_RANKING, "skill")
        assert result == [0]

    def test_filters_by_item_type(self) -> None:
        team_mixed = [
            {"item_name": "s-learn", "item_type": "skill", "uses": 10, "last_used_epoch": None},
            {"item_name": "s-learn", "item_type": "command", "uses": 99, "last_used_epoch": None},
        ]
        result = align_team_counts(["s-learn"], team_mixed, "skill")
        assert result == [10]  # command 側の 99 は無視される

    def test_empty_personal_labels(self) -> None:
        result = align_team_counts([], self.TEAM_RANKING, "skill")
        assert result == []
