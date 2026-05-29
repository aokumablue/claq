"""PostgreSQL 同期ロジックのテスト（モック使用）"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from devgear.mem.database import (
    Adr,
    Database,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
)
from devgear.mem.sync import SyncResult, _sync_embeddings, should_sync, sync_check, sync_to_postgres


@pytest.fixture
def mock_settings(tmp_path):
    """テスト用の設定オブジェクト"""
    settings = MagicMock()
    settings.db_path = str(tmp_path / "sync.db")
    settings.sync_lock_path = tmp_path / "sync.lock"
    settings.sync_state_path = tmp_path / "sync_state.json"
    settings.sync.enabled = True
    settings.sync.postgres_url = "postgresql://testuser@localhost:5432/testdb"
    settings.sync.interval_hours = 168  # 7日 = 168時間
    settings.sync.last_synced_at = 0.0
    settings.sync.last_sync_attempt_at = 0.0
    settings.sync.last_sync_success = False
    settings.reload_sync_state = lambda: None
    return settings


@pytest.fixture
def mock_git_user(monkeypatch):
    """sync.py 内の get_git_user_name を固定値に差し替えるフィクスチャ"""
    monkeypatch.setattr("devgear.mem.sync.get_git_user_name", lambda: "test_user")
    return "test_user"


class TestShouldSync:
    """should_sync 関数のテスト"""

    def test_returns_false_when_disabled(self, mock_settings):
        mock_settings.sync.enabled = False
        assert should_sync(mock_settings) is False

    def test_returns_false_when_no_postgres_url(self, mock_settings):
        mock_settings.sync.postgres_url = ""
        assert should_sync(mock_settings) is False

    def test_returns_true_when_never_synced(self, mock_settings):
        mock_settings.sync.last_synced_at = 0.0
        assert should_sync(mock_settings) is True

    def test_returns_false_when_within_interval(self, mock_settings):
        import time

        # 24時間前に同期した（168時間=7日間隔なので未達）
        mock_settings.sync.last_synced_at = time.time() - 24 * 60 * 60
        assert should_sync(mock_settings) is False

    def test_returns_true_when_interval_exceeded(self, mock_settings):
        import time

        # 169時間前に同期した（168時間間隔を超過）
        mock_settings.sync.last_synced_at = time.time() - 169 * 60 * 60
        assert should_sync(mock_settings) is True

    def test_returns_false_during_retry_backoff(self, mock_settings):
        import time

        now = time.time()
        mock_settings.sync.last_synced_at = now - 169 * 60 * 60
        mock_settings.sync.last_sync_success = False
        mock_settings.sync.last_sync_attempt_at = now - 60

        assert should_sync(mock_settings) is False


class TestSyncToPostgres:
    """sync_to_postgres 関数のテスト"""

    def test_returns_success_when_disabled(self, mock_settings):
        mock_settings.sync.enabled = False
        result = sync_to_postgres(mock_settings)
        assert result.success is True
        assert result.chunks == 0

    def test_returns_error_when_no_postgres_url(self, mock_settings):
        mock_settings.sync.postgres_url = ""
        result = sync_to_postgres(mock_settings)
        assert result.success is False
        assert "postgres_url" in result.error

    def test_pg_connection_failure_ignores_save_state_errors(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False

            def test_connection(self) -> bool:
                return False

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)
        monkeypatch.setattr(
            mock_settings,
            "save_sync_state",
            lambda: (_ for _ in ()).throw(RuntimeError("save failed")),
        )

        result = sync_to_postgres(mock_settings)

        assert result.success is False
        assert result.error == "PostgreSQL への接続に失敗しました"
        assert sqlite_db.closed is True
        assert pg_db.closed is True


class TestSyncCheck:
    """sync_check 関数のテスト"""

    def test_skips_when_not_needed(self, mock_settings):
        import time

        mock_settings.sync.last_synced_at = time.time()  # 今同期した
        result = sync_check(mock_settings)
        assert result.success is True
        assert result.chunks == 0

    def test_skips_when_disabled(self, mock_settings):
        mock_settings.sync.enabled = False
        result = sync_check(mock_settings)
        assert result.success is True


class TestSyncLocking:
    """ファイルロックによる同期制御のテスト"""

    def test_skips_when_another_sync_holds_lock(self, mock_settings, monkeypatch, mock_git_user):
        class FailingDatabase:
            def __init__(self, path):  # noqa: ANN001
                raise AssertionError("Database should not be opened when lock is held")

        class FailingPgDatabase:
            def __init__(self, url):  # noqa: ANN001
                raise AssertionError("PgDatabase should not be opened when lock is held")

        monkeypatch.setattr("devgear.mem.sync.Database", FailingDatabase)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", FailingPgDatabase)

        with mock_settings.sync_lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = sync_to_postgres(mock_settings)

        assert result.success is True
        assert result.chunks == 0

    def test_lock_release_allows_sync_to_proceed(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False
                self.tested = False

            def test_connection(self) -> bool:
                self.tested = True
                return True

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)

        with mock_settings.sync_lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        result = sync_to_postgres(mock_settings)

        assert result.success is True
        assert pg_db.tested is True
        assert sqlite_db.closed is True
        assert pg_db.closed is True

    def test_reloads_state_and_skips_duplicate_sync(self, mock_settings, monkeypatch):
        import time

        now = time.time()
        mock_settings.sync.last_synced_at = 0.0
        mock_settings.sync.last_sync_attempt_at = 0.0
        mock_settings.sync.last_sync_success = True

        mock_settings.sync_state_path.write_text(
            json.dumps(
                {
                    "last_synced_at": now,
                    "last_sync_attempt_at": now,
                    "last_sync_success": True,
                    "last_compacted_at": 0.0,
                }
            ),
            encoding="utf-8",
        )

        def reload_sync_state() -> None:
            raw = json.loads(mock_settings.sync_state_path.read_text(encoding="utf-8"))
            mock_settings.sync.last_synced_at = raw["last_synced_at"]
            mock_settings.sync.last_sync_attempt_at = raw["last_sync_attempt_at"]
            mock_settings.sync.last_sync_success = raw["last_sync_success"]

        mock_settings.reload_sync_state = reload_sync_state

        monkeypatch.setattr(
            "devgear.mem.sync.Database",
            lambda path: (_ for _ in ()).throw(AssertionError("Database should not be opened")),
        )
        monkeypatch.setattr(
            "devgear.mem.sync.PgDatabase",
            lambda url: (_ for _ in ()).throw(AssertionError("PgDatabase should not be opened")),
        )

        result = sync_to_postgres(mock_settings)

        assert result.success is True
        assert result.chunks == 0
        assert mock_settings.save_sync_state.called is False


class TestSyncResult:
    """SyncResult のテスト"""

    def test_default_values(self):
        result = SyncResult()
        assert result.chunks == 0
        assert result.sessions == 0
        assert result.instincts == 0
        assert result.adrs == 0
        assert result.events == 0
        assert result.success is True
        assert result.error is None

    def test_with_values(self):
        result = SyncResult(chunks=10, sessions=5, success=False, error="Test error")
        assert result.chunks == 10
        assert result.sessions == 5
        assert result.success is False
        assert result.error == "Test error"


class TestSyncToPostgresDetailed:
    """sync_to_postgres の詳細な分岐テスト"""

    def _seed_sync_rows(self, db: Database, *, include_synced_chunk: bool = False) -> dict[str, str]:
        session = Session(
            id="session-1",
            origin_user="sqlite-user",
            session_id="sess-1",
            project="proj",
            started_at_epoch=1700000000,
            chunk_count=0,
        )
        db.upsert_session(session)

        pending_chunk_id = db.store_chunk(
            MemoryChunk(
                id="chunk-1",
                origin_user="sqlite-user",
                session_id="sess-1",
                project="proj",
                chunk_index=0,
                content="content",
                tool_names=["Edit"],
                files_read=["src/app.py"],
                files_modified=["src/app.py"],
                user_prompt="prompt",
                created_at_epoch=1700000001,
            )
        )

        synced_chunk_id = ""
        if include_synced_chunk:
            synced_chunk_id = db.store_chunk(
                MemoryChunk(
                    id="chunk-2",
                    origin_user="sqlite-user",
                    session_id="sess-1",
                    project="proj",
                    chunk_index=1,
                    content="synced-content",
                    tool_names=["Read"],
                    files_read=["src/other.py"],
                    files_modified=[],
                    user_prompt="prompt-2",
                    created_at_epoch=1700000002,
                )
            )
            db.conn.execute(
                "UPDATE memory_chunks SET synced_at = ? WHERE id = ?",
                ("already-synced", synced_chunk_id),
            )

        db.upsert_instinct(
            Instinct(
                id="inst-1",
                instinct_id="inst-1",
                scope="project",
                confidence=0.8,
                content="content",
                created_at_epoch=1700000003,
                updated_at_epoch=1700000004,
                origin_user="sqlite-user",
                project_id="proj",
                trigger_text="when testing",
            )
        )
        db.upsert_adr(
            Adr(
                id="adr-1",
                project="proj",
                adr_number=1,
                title="title",
                status="accepted",
                content="content",
                created_at_epoch=1700000005,
                updated_at_epoch=1700000006,
                origin_user="sqlite-user",
            )
        )
        db.store_event_log(
            EventLog(
                id="event-1",
                event_type="notice",
                content="content",
                created_at_epoch=1700000007,
                origin_user="sqlite-user",
                project_id="proj",
            )
        )
        interaction_id = db.store_interaction_log(
            InteractionLog(
                id="interaction-1",
                origin_user="sqlite-user",
                session_id="sess-1",
                project="proj",
                user_prompt_full="do something",
                interaction_index=0,
                created_at_epoch=1700000008,
                user_prompt_hash="hash",
                ai_response_summary="summary",
                execution_outcome="success",
                tool_error_count=0,
            )
        )
        db.upsert_project_profile(
            ProjectProfile(
                id="profile-1",
                project="proj",
                detected_at_epoch=1700000009,
                last_updated_epoch=1700000010,
                origin_user="sqlite-user",
                project_path="/repo",
                languages=["python"],
                frameworks=["pytest"],
                primary_language="python",
                detection_confidence=0.9,
            )
        )
        db.store_mem_item_run(
            MemItemRun(
                id="run-1",
                session_id="sess-1",
                project="proj",
                skill_name="s-tdd",
                created_at_epoch=1700000011,
                origin_user="sqlite-user",
                skill_trigger="trigger",
                interaction_log_id=interaction_id,
            )
        )

        db.conn.commit()
        return {
            "pending_chunk_id": pending_chunk_id,
            "synced_chunk_id": synced_chunk_id,
            "session_id": "sess-1",
        }

    def test_dry_run_counts_only_unsynced_rows(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)
        ids = self._seed_sync_rows(db, include_synced_chunk=True)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False

            def test_connection(self) -> bool:
                return True

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)

        result = sync_to_postgres(mock_settings, dry_run=True)

        assert result.success is True
        assert result.chunks == 1
        assert result.sessions == 1
        assert result.instincts == 1
        assert result.adrs == 1
        assert result.events == 1
        assert result.interaction_logs == 1
        assert result.project_profiles == 1
        assert result.skill_runs == 1
        assert sqlite_db.closed is True
        assert pg_db.closed is True
        assert mock_settings.sync.last_synced_at == 0.0

        conn = sqlite3.connect(mock_settings.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, synced_at FROM memory_chunks WHERE id IN (?, ?) ORDER BY chunk_index",
            (ids["pending_chunk_id"], ids["synced_chunk_id"]),
        ).fetchall()
        assert rows[0]["synced_at"] is None
        assert rows[1]["synced_at"] == "already-synced"
        conn.close()

    def test_full_sync_claims_rows_and_commits(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)
        ids = self._seed_sync_rows(db)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False
                self.calls: list[tuple[str, object]] = []
                self.project_profiles = []
                self.skill_runs = []

            def test_connection(self) -> bool:
                return True

            def upsert_chunks_batch(self, chunks, origin_user):  # noqa: ANN001
                self.calls.append(("chunks", (origin_user, len(chunks))))
                return len(chunks)

            def upsert_sessions_batch(self, sessions, origin_user):  # noqa: ANN001
                self.calls.append(("sessions", (origin_user, len(sessions))))
                return len(sessions)

            def upsert_instincts_batch(self, instincts):  # noqa: ANN001
                self.calls.append(("instincts", instincts))
                return len(instincts)

            def upsert_adrs_batch(self, adrs):  # noqa: ANN001
                self.calls.append(("adrs", adrs))
                return len(adrs)

            def insert_event_logs_batch(self, events):  # noqa: ANN001
                self.calls.append(("events", events))
                return len(events)

            def upsert_interaction_logs_batch(self, logs):  # noqa: ANN001
                self.calls.append(("interaction_logs", logs))
                return len(logs)

            def upsert_project_profiles_batch(self, profiles):  # noqa: ANN001
                self.calls.append(("project_profiles", profiles))
                self.project_profiles = profiles
                return len(profiles)

            def upsert_mem_item_runs_batch(self, runs):  # noqa: ANN001
                self.calls.append(("skill_runs", runs))
                self.skill_runs = runs
                return len(runs)

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)
        monkeypatch.setattr("devgear.mem.sync._sync_embeddings", lambda sqlite_db, pg_db, chunks: len(chunks))
        monkeypatch.setattr("devgear.mem.sync.time.time", lambda: 10_000_000_000)

        result = sync_to_postgres(mock_settings, dry_run=False)

        assert result.success is True
        assert result.chunks == 1
        assert result.sessions == 1
        assert result.instincts == 1
        assert result.adrs == 1
        assert result.events == 1
        assert result.interaction_logs == 1
        assert result.project_profiles == 1
        assert result.skill_runs == 1
        assert result.embeddings == 1
        assert sqlite_db.closed is True
        assert pg_db.closed is True
        assert mock_settings.sync.last_synced_at == 10_000_000_000
        assert mock_settings.save_sync_state.called
        assert pg_db.calls[0] == ("chunks", ("test_user", 1))
        assert pg_db.calls[1] == ("sessions", ("test_user", 1))
        assert pg_db.project_profiles[0].origin_user == "test_user"
        assert pg_db.skill_runs[0].origin_user == "test_user"

        conn = sqlite3.connect(mock_settings.db_path)
        conn.row_factory = sqlite3.Row
        synced_rows = conn.execute(
            """
            SELECT synced_at
            FROM memory_chunks
            WHERE id = ? OR id = ?
            ORDER BY chunk_index
            """,
            (ids["pending_chunk_id"], ids["synced_chunk_id"] or ids["pending_chunk_id"]),
        ).fetchall()
        assert synced_rows[0]["synced_at"] is not None
        conn.close()

    def test_full_sync_without_chunks_uses_default_result(self, mock_settings, monkeypatch, mock_git_user):
        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False
                self.calls: list[str] = []

            def test_connection(self) -> bool:
                return True

            def upsert_chunks_batch(self, chunks, origin_user):  # noqa: ANN001
                self.calls.append("chunks")
                return len(chunks)

            def upsert_sessions_batch(self, sessions, origin_user):  # noqa: ANN001
                self.calls.append("sessions")
                return len(sessions)

            def upsert_instincts_batch(self, instincts):  # noqa: ANN001
                self.calls.append("instincts")
                return len(instincts)

            def upsert_adrs_batch(self, adrs):  # noqa: ANN001
                self.calls.append("adrs")
                return len(adrs)

            def insert_event_logs_batch(self, events):  # noqa: ANN001
                self.calls.append("events")
                return len(events)

            def upsert_interaction_logs_batch(self, logs):  # noqa: ANN001
                self.calls.append("interaction_logs")
                return len(logs)

            def upsert_project_profiles_batch(self, profiles):  # noqa: ANN001
                self.calls.append("project_profiles")
                return len(profiles)

            def upsert_mem_item_runs_batch(self, runs):  # noqa: ANN001
                self.calls.append("skill_runs")
                return len(runs)

            def close(self) -> None:
                self.closed = True

        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)

        result = sync_to_postgres(mock_settings, dry_run=False)

        assert result.success is True
        assert result.chunks == 0
        assert result.sessions == 0
        assert result.embeddings == 0
        assert pg_db.closed is True
        assert pg_db.calls == []
        assert mock_settings.sync.last_synced_at > 0
        assert mock_settings.save_sync_state.called

    def test_failed_sync_rolls_back_synced_at(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)
        ids = self._seed_sync_rows(db)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False

            def test_connection(self) -> bool:
                return True

            def upsert_chunks_batch(self, chunks, origin_user):  # noqa: ANN001
                raise RuntimeError("boom")

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)

        result = sync_to_postgres(mock_settings, dry_run=False)

        assert result.success is False
        assert "boom" in result.error
        assert sqlite_db.closed is True
        assert pg_db.closed is True
        assert mock_settings.sync.last_sync_success is False

        conn = sqlite3.connect(mock_settings.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT synced_at FROM memory_chunks WHERE id = ?",
            (ids["pending_chunk_id"],),
        ).fetchone()
        assert row["synced_at"] is None
        session_row = conn.execute(
            "SELECT synced_at FROM sessions WHERE session_id = ?",
            (ids["session_id"],),
        ).fetchone()
        assert session_row["synced_at"] is None
        conn.close()

    def test_failed_sync_ignores_save_state_errors(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)
        self._seed_sync_rows(db)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False

            def test_connection(self) -> bool:
                return True

            def upsert_chunks_batch(self, chunks, origin_user):  # noqa: ANN001
                raise RuntimeError("boom")

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)
        monkeypatch.setattr(
            mock_settings,
            "save_sync_state",
            lambda: (_ for _ in ()).throw(RuntimeError("save failed")),
        )

        result = sync_to_postgres(mock_settings, dry_run=False)

        assert result.success is False
        assert "boom" in result.error
        assert sqlite_db.closed is True
        assert pg_db.closed is True
        assert mock_settings.sync.last_sync_success is False

    def test_pg_connection_failure(self, mock_settings, monkeypatch, mock_git_user):
        db = Database(mock_settings.db_path)

        class FakeSQLiteDb:
            def __init__(self, real_db: Database) -> None:
                self.conn = real_db.conn
                self._real_db = real_db
                self.closed = False

            def close(self) -> None:
                self.closed = True
                self._real_db.close()

        class FakePgDb:
            def __init__(self) -> None:
                self.closed = False

            def test_connection(self) -> bool:
                return False

            def close(self) -> None:
                self.closed = True

        sqlite_db = FakeSQLiteDb(db)
        pg_db = FakePgDb()
        monkeypatch.setattr("devgear.mem.sync.Database", lambda path: sqlite_db)
        monkeypatch.setattr("devgear.mem.sync.PgDatabase", lambda url: pg_db)

        result = sync_to_postgres(mock_settings, dry_run=False)

        assert result.success is False
        assert result.error == "PostgreSQL への接続に失敗しました"
        assert sqlite_db.closed is True
        assert pg_db.closed is True

    def test_sync_check_delegates_when_needed(self, mock_settings, monkeypatch):
        monkeypatch.setattr("devgear.mem.sync.should_sync", lambda settings: True)
        monkeypatch.setattr("devgear.mem.sync.sync_to_postgres", lambda settings, dry_run=False: SyncResult(chunks=3))

        result = sync_check(mock_settings)
        assert result.chunks == 3


class TestSyncEmbeddings:
    """_sync_embeddings のテスト"""

    def test_handles_empty_and_error_paths(self, monkeypatch):
        class FakePgDb:
            def upsert_embeddings_batch(self, embeddings):  # noqa: ANN001
                return len(embeddings)

        class FakeConn:
            def execute(self, sql: str, params=None):  # noqa: ANN001
                raise RuntimeError("boom")

        class FakeSQLiteDb:
            def __init__(self) -> None:
                self.conn = FakeConn()

        assert _sync_embeddings(FakeSQLiteDb(), FakePgDb(), []) == 0
        assert _sync_embeddings(FakeSQLiteDb(), FakePgDb(), [MemoryChunk("s", "p", 0, "c", [], [], [], "", 1, id="c1")]) == 0

    def test_returns_zero_when_no_embeddings_are_found(self):
        class FakePgDb:
            def upsert_embeddings_batch(self, embeddings):  # noqa: ANN001
                return len(embeddings)

        class FakeConn:
            def execute(self, sql: str, params=None):  # noqa: ANN001
                return SimpleNamespace(fetchall=lambda: [])

        class FakeSQLiteDb:
            def __init__(self) -> None:
                self.conn = FakeConn()

        assert _sync_embeddings(
            FakeSQLiteDb(),
            FakePgDb(),
            [MemoryChunk("s", "p", 0, "c", [], [], [], "", 1, id="c1")],
        ) == 0

    def test_decodes_embeddings_when_available(self):
        class FakePgDb:
            def __init__(self) -> None:
                self.embeddings = None

            def upsert_embeddings_batch(self, embeddings):  # noqa: ANN001
                self.embeddings = embeddings
                return len(embeddings)

        class FakeConn:
            def execute(self, sql: str, params=None):  # noqa: ANN001
                return SimpleNamespace(fetchall=lambda: [("c1", struct.pack("2f", 0.1, 0.2))])

        class FakeSQLiteDb:
            def __init__(self) -> None:
                self.conn = FakeConn()

        pg = FakePgDb()
        count = _sync_embeddings(FakeSQLiteDb(), pg, [MemoryChunk("s", "p", 0, "c", [], [], [], "", 1, id="c1")])
        assert count == 1
        assert pg.embeddings[0][0] == "c1"
        assert pg.embeddings[0][1][0] == pytest.approx(0.1)
        assert pg.embeddings[0][1][1] == pytest.approx(0.2)


class TestSyncHelpers:
    """内部ヘルパーの分岐テスト"""

    def test_count_pending_rows_returns_zero_for_empty_table(self, tmp_path):
        db = Database(tmp_path / "empty-sync.db")
        from devgear.mem.sync import _count_pending_rows

        assert _count_pending_rows(db.conn, "memory_chunks") == 0
        db.close()

    def test_count_pending_embeddings_handles_empty_and_operational_error(self):
        from devgear.mem.sync import _count_pending_embeddings

        class EmptyConn:
            def execute(self, sql: str, params=None):  # noqa: ANN001
                raise sqlite3.OperationalError("no vec table")

        class OkConn:
            def execute(self, sql: str, params=None):  # noqa: ANN001
                return SimpleNamespace(fetchone=lambda: (0,))

        assert _count_pending_embeddings(OkConn(), []) == 0
        assert _count_pending_embeddings(EmptyConn(), ["c1"]) == 0


class TestSyncLogVisibility:
    """スキップ・失敗時のログレベルが info/error になっていることを検証するテスト。
    処理継続性（例外が外に出ないこと）も合わせて確認する。
    """

    def test_sync_check_logs_info_on_skip_disabled(self, mock_settings, caplog):
        """enabled=False のスキップ時に info ログが出ることを確認する。"""
        import logging

        mock_settings.sync.enabled = False
        # get_logger("SYNC") は "devgear.mem.SYNC" を返す
        with caplog.at_level(logging.INFO, logger="devgear.mem.SYNC"):
            result = sync_check(mock_settings)
        assert result.success is True
        assert any("スキップ" in r.message and r.levelno == logging.INFO for r in caplog.records)

    def test_sync_check_skips_when_no_url(self, mock_settings, caplog):
        """postgres_url 未設定の場合 sync_check は success=True でスキップする。"""
        import logging

        mock_settings.sync.postgres_url = ""
        # should_sync が False を返すため sync_check レベルで "スキップ" info ログが出る
        with caplog.at_level(logging.INFO, logger="devgear.mem.SYNC"):
            result = sync_check(mock_settings)
        assert result.success is True
        assert any("スキップ" in r.message and r.levelno == logging.INFO for r in caplog.records)

    def test_sync_to_postgres_logs_info_on_no_url(self, mock_settings, caplog):
        """sync_to_postgres で postgres_url 未設定時に info ログが出ることを確認する。"""
        import logging

        mock_settings.sync.postgres_url = ""
        with caplog.at_level(logging.INFO, logger="devgear.mem.SYNC"):
            result = sync_to_postgres(mock_settings)
        assert result.success is False
        assert any("postgres_url" in r.message and r.levelno == logging.INFO for r in caplog.records)

    def test_sync_to_postgres_logs_error_on_connection_failure(self, mock_settings, monkeypatch, caplog):
        """PG 接続失敗時に error ログが出て、かつ処理が継続する（例外が出ない）ことを確認する。"""
        import logging

        class FakePgDb:
            def __init__(self, url, **kwargs):  # noqa: ANN001
                pass

            def test_connection(self):
                return False

            def close(self):
                pass

        class FakeDatabase:
            def __init__(self, path):  # noqa: ANN001
                pass

            def close(self):
                pass

        monkeypatch.setattr("devgear.mem.sync.PgDatabase", FakePgDb)
        monkeypatch.setattr("devgear.mem.sync.Database", FakeDatabase)

        with caplog.at_level(logging.ERROR, logger="devgear.mem.SYNC"):
            result = sync_to_postgres(mock_settings)
        # 処理は継続し例外は出ない
        assert result.success is False
        assert result.error == "PostgreSQL への接続に失敗しました"
        assert any("PG 接続失敗" in r.message and r.levelno == logging.ERROR for r in caplog.records)

    def test_sync_to_postgres_logs_error_with_traceback_on_exception(
        self, mock_settings, monkeypatch, caplog
    ):
        """例外発生時に error + exc_info が出て、プロセスが継続することを確認する。"""
        import logging

        class BoomDatabase:
            def __init__(self, path):  # noqa: ANN001
                raise RuntimeError("DB 接続失敗")

        monkeypatch.setattr("devgear.mem.sync.Database", BoomDatabase)

        with caplog.at_level(logging.ERROR, logger="devgear.mem.SYNC"):
            result = sync_to_postgres(mock_settings)
        assert result.success is False
        # result.error は _mask_url を通すためパスワード断片を含まない
        assert result.error is not None
        assert "TESTPASSWORD" not in (result.error or "")
        # exc_info=True が付いているので traceback が caplog に含まれる
        error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert error_records, "error レベルのログが出ていない"
        assert error_records[-1].exc_info is not None

    def test_resolve_sync_lock_path_falls_back_when_path_invalid(self, monkeypatch):
        """sync_lock_path が Path 化できないオブジェクトのとき、HOME 配下にフォールバックする（lines 134-135）。"""
        from devgear.mem.sync import _resolve_sync_lock_path

        class _Bad:
            def __fspath__(self):
                raise TypeError("not path-like")

        settings = SimpleNamespace(sync_lock_path=_Bad())
        result = _resolve_sync_lock_path(settings)
        assert str(result).endswith(".devgear/sync.lock")

    def test_acquire_sync_lock_reraises_unexpected_oserror(self, tmp_path, monkeypatch):
        """flock が EACCES/EAGAIN 以外で失敗した場合は例外を再発火する（line 150）。"""
        import devgear.mem.sync as sync_mod

        settings = SimpleNamespace(sync_lock_path=tmp_path / "sync.lock")

        def _flock(_fd, _flags):
            raise OSError(13, "fake error")  # 13 = EACCES でなく EPERM 等の代理として

        # EACCES の番号と被らないよう、別の errno を使う
        import errno as _errno

        def _flock_unexpected(_fd, _flags):
            err = OSError("unexpected")
            err.errno = _errno.EIO
            raise err

        monkeypatch.setattr(sync_mod.fcntl, "flock", _flock_unexpected)

        with pytest.raises(OSError):
            with sync_mod._acquire_sync_lock(settings):
                pass

    def test_reload_sync_state_falls_back_to_private_loader(self, monkeypatch):
        """reload_sync_state が無く _load_sync_state が呼ばれる経路（lines 164-166）。"""
        from devgear.mem.sync import _reload_sync_state

        called = {"v": False}

        class _S:
            def _load_sync_state(self) -> None:
                called["v"] = True

        _reload_sync_state(_S())
        assert called["v"] is True

    def test_reload_sync_state_noop_when_no_methods(self) -> None:
        """reload_sync_state も _load_sync_state も無いなら何もしない。"""
        from devgear.mem.sync import _reload_sync_state

        _reload_sync_state(SimpleNamespace())

    def test_mask_url(self, monkeypatch):
        """_mask_url がパスワード部を正しくマスクすることを確認する。"""
        from devgear.mem.sync import _mask_url

        assert _mask_url("postgresql://user:secret@host:5432/db") == "postgresql://user:***@host:5432/db"
        assert _mask_url("postgresql://user@host/db") == "postgresql://user@host/db"
        assert _mask_url("") == ""
        # @ を含む生パスワードでも正しくマスクされる
        assert _mask_url("postgresql://user:p@ss@host/db") == "postgresql://user:***@host/db"
        # 空パスワードは urlparse が password="" と認識しマスクされる
        masked_empty = _mask_url("postgresql://user:@host/db")
        assert "user:" in masked_empty
        assert "@host" in masked_empty
        # urlparse が例外を投げる場合は元の URL をそのまま返す
        import devgear.mem.sync as sync_mod

        monkeypatch.setattr(sync_mod, "urlparse", lambda url: (_ for _ in ()).throw(ValueError("parse error")))
        assert _mask_url("postgresql://user:secret@host/db") == "postgresql://user:secret@host/db"
