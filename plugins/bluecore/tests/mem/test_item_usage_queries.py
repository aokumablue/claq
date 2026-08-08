"""item_usage_queries モジュールのテスト"""

from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

import pytest

from bluecore.mem.database import Database, MemItemRun
from bluecore.mem.item_usage_queries import (
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
        ("learn", "skill", "success", now - 100),
        ("learn", "skill", "success", now - 200),
        ("learn", "skill", "failure", now - 300),
        ("tdd", "skill", "success", now - 150),
        ("dashboard", "command", "success", now - 50),
        ("dashboard", "command", "unknown", now - 400),
        ("reviewer", "agent", "success", now - 80),
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
        result = item_usage_ranking(db_with_records, days=365)
        assert len(result) == 4  # learn, tdd, dashboard, reviewer

    def test_sorted_by_uses_desc(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, days=365)
        uses = [r["uses"] for r in result]
        assert uses == sorted(uses, reverse=True)

    def test_correct_item_type(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, days=365)
        type_map = {r["item_name"]: r["item_type"] for r in result}
        assert type_map["learn"] == "skill"
        assert type_map["dashboard"] == "command"
        assert type_map["reviewer"] == "agent"

    def s_learn_uses_count(self, db_with_records: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_with_records, days=365)
        skill = next(r for r in result if r["item_name"] == "learn")
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
        result = item_usage_ranking(db_conn, days=30)
        names = [r["item_name"] for r in result]
        assert "s-old" not in names

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = item_usage_ranking(db_conn, days=30)
        assert result == []


# --- daily_trend ---


class TestDailyTrend:
    def test_returns_list(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, days=365)
        assert isinstance(result, list)

    def test_has_required_keys(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, days=365)
        if result:
            row = result[0]
            assert set(row.keys()) == {"date", "skill", "command", "agent", "total"}

    def test_total_equals_sum(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, days=365)
        for row in result:
            assert row["total"] == row["skill"] + row["command"] + row["agent"]

    def test_dates_sorted(self, db_with_records: sqlite3.Connection) -> None:
        result = daily_trend(db_with_records, days=365)
        dates = [r["date"] for r in result]
        assert dates == sorted(dates)

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = daily_trend(db_conn, days=30)
        assert result == []


# --- outcome_distribution ---


class TestOutcomeDistribution:
    def test_returns_all_outcomes(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, days=365)
        outcomes = {r["outcome"] for r in result}
        assert outcomes == {"success", "failure", "unknown"}

    def test_success_count(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, days=365)
        success = next(r for r in result if r["outcome"] == "success")
        assert success["count"] == 5  # 7件中 success が5件

    def test_sorted_by_count_desc(self, db_with_records: sqlite3.Connection) -> None:
        result = outcome_distribution(db_with_records, days=365)
        counts = [r["count"] for r in result]
        assert counts == sorted(counts, reverse=True)

    def test_empty_db(self, db_conn: sqlite3.Connection) -> None:
        result = outcome_distribution(db_conn, days=30)
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
            skill_name="dashboard",
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


# --- make_ranking_data ---


class TestMakeRankingData:
    """make_ranking_data のユニットテスト。"""

    RANKING = [
        {"item_name": "learn", "item_type": "skill", "uses": 5, "last_used_epoch": None},
        {"item_name": "tdd", "item_type": "skill", "uses": 2, "last_used_epoch": None},
        {"item_name": "dashboard", "item_type": "command", "uses": 3, "last_used_epoch": None},
        {"item_name": "reviewer", "item_type": "agent", "uses": 1, "last_used_epoch": None},
    ]

    def test_skill_labels_and_counts(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "skill")
        assert labels == ["learn", "tdd"]
        assert counts == [5, 2]

    def test_command_labels_and_counts(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "command")
        assert labels == ["dashboard"]
        assert counts == [3]

    def test_empty_type(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "agent")
        assert labels == ["reviewer"]
        assert counts == [1]

    def test_no_match_returns_empty(self) -> None:
        labels, counts = make_ranking_data(self.RANKING, "unknown_type")
        assert labels == []
        assert counts == []
