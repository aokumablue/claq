"""state store のマイグレーションに対するテスト。"""

from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from devgear.lib.state_store.migrations import (
    MIGRATIONS,
    apply_migrations,
    ensure_migration_table,
    get_applied_migrations,
)


@pytest.fixture
def memory_db():
    """インメモリ SQLite データベースを作成する。"""
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


class TestEnsureMigrationTable:
    """ensure_migration_table のテスト。"""

    def test_creates_table_when_not_exists(self, memory_db):
        """schema_migrations テーブルがなければ作成すること。"""
        ensure_migration_table(memory_db)
        cursor = memory_db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        assert cursor.fetchone() is not None

    def test_idempotent(self, memory_db):
        """複数回呼び出しても安全なこと。"""
        ensure_migration_table(memory_db)
        ensure_migration_table(memory_db)
        cursor = memory_db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='schema_migrations'")
        assert cursor.fetchone()[0] == 1


class TestGetAppliedMigrations:
    """get_applied_migrations のテスト。"""

    def test_returns_empty_list_initially(self, memory_db):
        """適用済みマイグレーションがない場合は空リストを返すこと。"""
        result = get_applied_migrations(memory_db)
        assert result == []

    def test_returns_applied_migrations(self, memory_db):
        """適用済みマイグレーション一覧を返すこと。"""
        ensure_migration_table(memory_db)
        now = datetime.now().isoformat()
        memory_db.execute(
            "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
            (1, "test_migration", now),
        )
        memory_db.commit()

        result = get_applied_migrations(memory_db)
        assert len(result) == 1
        assert result[0]["version"] == 1
        assert result[0]["name"] == "test_migration"
        assert result[0]["applied_at"] == now


class TestApplyMigrations:
    """apply_migrations のテスト。"""

    def test_applies_initial_migration(self, memory_db):
        """初期マイグレーションを適用してテーブルを作成すること。"""
        result = apply_migrations(memory_db)
        assert len(result) >= 1
        assert result[0]["version"] == 1

        # テーブルが存在することを確認
        cursor = memory_db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        tables = {row[0] for row in cursor.fetchall()}
        expected_tables = {
            "schema_migrations",
            "sessions",
            "skill_runs",
            "skill_versions",
            "decisions",
            "install_state",
            "governance_events",
        }
        assert expected_tables.issubset(tables)

    def test_idempotent(self, memory_db):
        """複数回呼び出しても安全なこと。"""
        apply_migrations(memory_db)
        apply_migrations(memory_db)
        result = get_applied_migrations(memory_db)
        assert len(result) == len(MIGRATIONS)

    def test_skips_already_applied(self, memory_db):
        """既に適用済みのマイグレーションはスキップすること。"""
        # マイグレーションを適用
        apply_migrations(memory_db)

        # 適用済み件数を記録
        initial_result = get_applied_migrations(memory_db)
        initial_count = len(initial_result)

        # 再適用
        apply_migrations(memory_db)
        final_result = get_applied_migrations(memory_db)

        assert len(final_result) == initial_count

    def test_creates_sessions_table_with_correct_schema(self, memory_db):
        """sessions テーブルが正しい列構成で作成されること。"""
        apply_migrations(memory_db)
        cursor = memory_db.execute("PRAGMA table_info(sessions)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "adapter_id" in columns
        assert "harness" in columns
        assert "state" in columns
        assert "repo_root" in columns
        assert "started_at" in columns
        assert "ended_at" in columns
        assert "snapshot" in columns

    def test_creates_skill_runs_table_with_correct_schema(self, memory_db):
        """skill_runs テーブルが正しい列構成で作成されること。"""
        apply_migrations(memory_db)
        cursor = memory_db.execute("PRAGMA table_info(skill_runs)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "skill_id" in columns
        assert "skill_version" in columns
        assert "session_id" in columns
        assert "task_description" in columns
        assert "outcome" in columns
        assert "failure_reason" in columns
        assert "tokens_used" in columns
        assert "duration_ms" in columns
        assert "user_feedback" in columns
        assert "created_at" in columns

    def test_creates_decisions_table_with_correct_schema(self, memory_db):
        """decisions テーブルが正しい列構成で作成されること。"""
        apply_migrations(memory_db)
        cursor = memory_db.execute("PRAGMA table_info(decisions)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}

        assert "id" in columns
        assert "session_id" in columns
        assert "title" in columns
        assert "rationale" in columns
        assert "alternatives" in columns
        assert "supersedes" in columns
        assert "status" in columns
        assert "created_at" in columns

    def test_creates_indexes(self, memory_db):
        """適切なインデックスを作成すること。"""
        apply_migrations(memory_db)
        cursor = memory_db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        indexes = {row[0] for row in cursor.fetchall()}

        # 期待するインデックスの一部を確認
        expected_indexes = {
            "idx_sessions_state_started_at",
            "idx_sessions_started_at",
            "idx_skill_runs_session_id_created_at",
            "idx_skill_runs_created_at",
        }
        assert expected_indexes.issubset(indexes)


class TestMigrationsStructure:
    """マイグレーションデータ構造のテスト。"""

    def test_migrations_have_required_fields(self):
        """すべてのマイグレーションが version・name・sql を持つこと。"""
        for migration in MIGRATIONS:
            assert "version" in migration
            assert "name" in migration
            assert "sql" in migration

    def test_migration_versions_are_sequential(self):
        """マイグレーションの version が 1 から連番であること。"""
        versions = [m["version"] for m in MIGRATIONS]
        assert versions == list(range(1, len(MIGRATIONS) + 1))

    def test_migration_names_are_unique(self):
        """マイグレーション名が一意であること。"""
        names = [m["name"] for m in MIGRATIONS]
        assert len(names) == len(set(names))
