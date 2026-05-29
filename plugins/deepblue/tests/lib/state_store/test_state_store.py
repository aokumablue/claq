"""state store のメインインターフェースに対するテスト。"""

from __future__ import annotations

import sqlite3

import pytest

from devgear.lib.state_store import (
    create_state_store,
)


class TestCreateStateStore:
    """create_state_store ファクトリー関数のテスト。"""

    def test_creates_memory_db_by_default(self):
        """パス未指定時にインメモリ DB を作成すること。"""
        store = create_state_store()
        try:
            assert store.is_memory is True
            assert store.db_path is None
        finally:
            store.close()

    def test_creates_memory_db_with_explicit_memory(self):
        """:memory: パス指定でインメモリ DB を作成すること。"""
        store = create_state_store(":memory:")
        try:
            assert store.is_memory is True
        finally:
            store.close()

    def test_creates_file_based_db(self, tmp_path):
        """ファイルベースの DB を作成すること。"""
        db_path = tmp_path / "test.db"
        store = create_state_store(db_path)
        try:
            assert store.is_memory is False
            assert store.db_path == str(db_path)
            assert db_path.exists()
        finally:
            store.close()

    def test_creates_parent_directories(self, tmp_path):
        """DB ファイル用の親ディレクトリを作成すること。"""
        db_path = tmp_path / "nested" / "dir" / "test.db"
        store = create_state_store(db_path)
        try:
            assert db_path.parent.exists()
        finally:
            store.close()

    def test_auto_applies_migrations(self):
        """デフォルトでマイグレーションを適用すること。"""
        store = create_state_store()
        try:
            # データを挿入できること
            store.upsert_session(
                {
                    "id": "test",
                    "adapterId": "test",
                    "harness": "cli",
                    "state": "active",
                    "snapshot": {},
                }
            )
            result = store.get_session_by_id("test")
            assert result is not None
        finally:
            store.close()

    def test_skips_migrations_when_disabled(self):
        """auto_migrate=False のときマイグレーションをスキップすること。"""
        store = create_state_store(auto_migrate=False)
        try:
            # テーブルがないため失敗すること
            with pytest.raises(sqlite3.OperationalError):
                store.get_session_by_id("test")
        finally:
            store.close()


class TestStateStore:
    """StateStore クラスのテスト。"""

    @pytest.fixture
    def store(self):
        """テスト用の state store を作成する。"""
        store = create_state_store()
        yield store
        store.close()

    @pytest.fixture
    def sample_session(self):
        """サンプルの session dict を作成する。"""
        return {
            "id": "session-1",
            "adapterId": "claude-history",
            "harness": "cli",
            "state": "active",
            "repoRoot": "/home/user/project",
            "startedAt": "2024-01-01T10:00:00Z",
            "snapshot": {"workers": []},
        }

    def test_session_crud(self, store, sample_session):
        """session の CRUD 操作をサポートすること。"""
        # 作成
        created = store.upsert_session(sample_session)
        assert created.id == "session-1"

        # 取得
        read = store.get_session_by_id("session-1")
        assert read.adapter_id == "claude-history"

        # 更新
        sample_session["state"] = "completed"
        updated = store.upsert_session(sample_session)
        assert updated.state == "completed"

        # 一覧
        result = store.list_recent_sessions()
        assert result["totalCount"] == 1

        # 詳細
        detail = store.get_session_detail("session-1")
        assert detail["session"].id == "session-1"

    def test_skill_run_operations(self, store, sample_session):
        """skill run 操作をサポートすること。"""
        store.upsert_session(sample_session)

        run = store.insert_skill_run(
            {
                "id": "run-1",
                "skillId": "tdd",
                "skillVersion": "1.0",
                "sessionId": "session-1",
                "taskDescription": "Task",
                "outcome": "success",
            }
        )

        assert run.id == "run-1"
        assert run.outcome == "success"

    def test_skill_version_operations(self, store):
        """skill version 操作をサポートすること。"""
        version = store.upsert_skill_version(
            {
                "skillId": "tdd",
                "version": "1.0",
                "contentHash": "abc123",
            }
        )

        assert version.skill_id == "tdd"
        assert version.content_hash == "abc123"

    def test_decision_operations(self, store, sample_session):
        """decision 操作をサポートすること。"""
        store.upsert_session(sample_session)

        decision = store.insert_decision(
            {
                "id": "dec-1",
                "sessionId": "session-1",
                "title": "Use JWT",
                "rationale": "Standard",
                "alternatives": [],
                "status": "accepted",
            }
        )

        assert decision.id == "dec-1"
        assert decision.title == "Use JWT"

    def test_install_state_operations(self, store):
        """install state 操作をサポートすること。"""
        state = store.upsert_install_state(
            {
                "targetId": "home",
                "targetRoot": "/home/user/.claude",
                "modules": ["tdd"],
                "operations": [],
                "sourceVersion": "1.0",
            }
        )

        assert state.target_id == "home"
        assert state.module_count == 1

    def test_governance_event_operations(self, store, sample_session):
        """governance event 操作をサポートすること。"""
        store.upsert_session(sample_session)

        event = store.insert_governance_event(
            {
                "id": "event-1",
                "sessionId": "session-1",
                "eventType": "warning",
                "payload": {"msg": "test"},
            }
        )

        assert event.id == "event-1"
        assert event.event_type == "warning"

    def test_get_status(self, store, sample_session):
        """包括的なステータスを返すこと。"""
        store.upsert_session(sample_session)

        status = store.get_status()

        assert "generatedAt" in status
        assert "activeSessions" in status
        assert "skillRuns" in status
        assert "installHealth" in status
        assert "governance" in status

    def test_save_for_memory_db(self, store, sample_session):
        """メモリ DB 保存時に失敗しないこと。"""
        store.upsert_session(sample_session)
        store.save()  # 例外が発生しないこと

    def test_save_for_file_db(self, tmp_path):
        """ファイル DB で変更をコミットすること。"""
        db_path = tmp_path / "test.db"
        store = create_state_store(db_path)
        try:
            store.upsert_session(
                {
                    "id": "test",
                    "adapterId": "test",
                    "harness": "cli",
                    "state": "active",
                    "snapshot": {},
                }
            )
            store.save()

            # 再オープンしてデータ永続化を確認
            store.close()
            store2 = create_state_store(db_path)
            try:
                result = store2.get_session_by_id("test")
                assert result is not None
            finally:
                store2.close()
        finally:
            if not store._closed:
                store.close()

    def test_close_is_idempotent(self, store):
        """複数回 close しても安全なこと。"""
        store.close()
        store.close()  # 例外が発生しないこと


class TestPersistence:
    """データベース永続化のテスト。"""

    def test_data_persists_across_connections(self, tmp_path):
        """DB 再オープン時にデータが保持されること。"""
        db_path = tmp_path / "test.db"

        # 作成して投入
        store1 = create_state_store(db_path)
        store1.upsert_session(
            {
                "id": "persist-test",
                "adapterId": "test",
                "harness": "cli",
                "state": "active",
                "snapshot": {"key": "value"},
            }
        )
        store1.close()

        # 再オープンして確認
        store2 = create_state_store(db_path)
        try:
            result = store2.get_session_by_id("persist-test")
            assert result is not None
            assert result.id == "persist-test"
            assert result.snapshot == {"key": "value"}
        finally:
            store2.close()

    def test_migrations_not_reapplied(self, tmp_path):
        """再オープン時にマイグレーションが再適用されないこと。"""
        db_path = tmp_path / "test.db"

        # 初回オープンでマイグレーション適用
        store1 = create_state_store(db_path)
        store1.close()

        # 2 回目のオープンで失敗しないこと
        store2 = create_state_store(db_path)
        try:
            # 通常動作すること
            store2.upsert_session(
                {
                    "id": "test",
                    "adapterId": "test",
                    "harness": "cli",
                    "state": "active",
                    "snapshot": {},
                }
            )
        finally:
            store2.close()


class TestPathTypes:
    """異なるパスタイプのテスト。"""

    def test_accepts_string_path(self, tmp_path):
        """文字列パスを受け付けること。"""
        db_path = str(tmp_path / "test.db")
        store = create_state_store(db_path)
        try:
            assert store.db_path == db_path
        finally:
            store.close()

    def test_accepts_path_object(self, tmp_path):
        """Path オブジェクトを受け付けること。"""
        db_path = tmp_path / "test.db"
        store = create_state_store(db_path)
        try:
            assert store.db_path == str(db_path)
        finally:
            store.close()

    def test_accepts_none(self):
        """インメモリ指定として None を受け付けること。"""
        store = create_state_store(None)
        try:
            assert store.is_memory is True
        finally:
            store.close()
