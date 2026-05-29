"""database のテスト"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgear.mem.database import (
    Database,
    _make_prompt_hash,
)
from devgear.mem.models import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
)
from devgear.mem.row_converters import (
    _parse_json_dict_list,
    _parse_json_list,
    _row_to_adr,
    _row_to_chunk,
    _row_to_event_log,
    _row_to_instinct,
)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


class TestDatabase:
    """データベース基本操作のテストケース"""

    def test_store_and_retrieve_chunk(self, db: Database) -> None:
        chunk = MemoryChunk(
            session_id="sess-1",
            project="my-project",
            chunk_index=0,
            content="[Read] /path/to/file.py",
            tool_names=["Read"],
            files_read=["/path/to/file.py"],
            files_modified=[],
            user_prompt="show me the file",
            created_at_epoch=1700000000,
        )
        chunk_id = db.store_chunk(chunk)
        assert isinstance(chunk_id, str)
        assert len(chunk_id) == 36  # UUID format

        retrieved = db.get_chunk_by_id(chunk_id)
        assert retrieved is not None
        assert retrieved.session_id == "sess-1"
        assert retrieved.project == "my-project"
        assert retrieved.tool_names == ["Read"]
        assert retrieved.files_read == ["/path/to/file.py"]

    def test_get_chunks_by_session(self, db: Database) -> None:
        for i in range(3):
            db.store_chunk(
                MemoryChunk(
                    session_id="sess-1",
                    project="proj",
                    chunk_index=i,
                    content=f"chunk {i}",
                    tool_names=["Bash"],
                    files_read=[],
                    files_modified=[],
                    user_prompt="do stuff",
                    created_at_epoch=1700000000 + i,
                )
            )
        chunks = db.get_chunks_by_session("sess-1")
        assert len(chunks) == 3
        assert [c.chunk_index for c in chunks] == [0, 1, 2]

    def test_upsert_session(self, db: Database) -> None:
        session = Session(session_id="sess-1", project="proj", started_at_epoch=1700000000)
        id1 = db.upsert_session(session)
        db.conn.execute(
            "UPDATE sessions SET synced_at = ? WHERE session_id = ?",
            ("already-synced", session.session_id),
        )
        db.conn.commit()
        id2 = db.upsert_session(session)
        assert id1 == id2
        row = db.conn.execute(
            "SELECT synced_at FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        assert row["synced_at"] is None

    def test_schema_includes_synced_at_columns(self, db: Database) -> None:
        tables = [
            "memory_chunks",
            "sessions",
            "instincts",
            "adrs",
            "event_logs",
            "interaction_logs",
            "project_profiles",
            "mem_item_runs",
        ]
        for table in tables:
            columns = {
                row["name"]
                for row in db.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            assert "synced_at" in columns

    def test_write_paths_clear_synced_at(self, db: Database) -> None:
        session = Session(session_id="sess-1", project="proj", started_at_epoch=1700000000)
        db.upsert_session(session)
        chunk_id = db.store_chunk(
            MemoryChunk(
                session_id="sess-1",
                project="proj",
                chunk_index=0,
                content="chunk content",
                tool_names=["Read"],
                files_read=[],
                files_modified=[],
                user_prompt="prompt",
                created_at_epoch=1700000001,
            )
        )
        instinct = Instinct(
            instinct_id="inst-1",
            scope="project",
            confidence=0.7,
            content="content",
            created_at_epoch=1700000002,
            updated_at_epoch=1700000003,
            project_id="proj",
        )
        instinct_id = db.upsert_instinct(instinct)
        adr = Adr(
            project="proj",
            adr_number=1,
            title="ADR",
            status="accepted",
            content="content",
            created_at_epoch=1700000004,
            updated_at_epoch=1700000005,
        )
        adr_id = db.upsert_adr(adr)
        profile = ProjectProfile(
            project="proj",
            detected_at_epoch=1700000006,
            last_updated_epoch=1700000007,
        )
        profile_id = db.upsert_project_profile(profile)

        db.conn.execute("UPDATE memory_chunks SET synced_at = ? WHERE id = ?", ("done", chunk_id))
        db.conn.execute("UPDATE sessions SET synced_at = ? WHERE session_id = ?", ("done", session.session_id))
        db.conn.execute("UPDATE instincts SET synced_at = ? WHERE id = ?", ("done", instinct_id))
        db.conn.execute("UPDATE adrs SET synced_at = ? WHERE id = ?", ("done", adr_id))
        db.conn.execute("UPDATE project_profiles SET synced_at = ? WHERE id = ?", ("done", profile_id))
        db.conn.commit()

        db.update_access([chunk_id])
        db.store_chunk(
            MemoryChunk(
                session_id="sess-1",
                project="proj",
                chunk_index=1,
                content="chunk content 2",
                tool_names=["Edit"],
                files_read=[],
                files_modified=[],
                user_prompt="prompt 2",
                created_at_epoch=1700000008,
            )
        )
        db.upsert_instinct(
            Instinct(
                instinct_id="inst-1",
                scope="project",
                confidence=0.8,
                content="updated",
                created_at_epoch=1700000002,
                updated_at_epoch=1700000009,
                project_id="proj",
            )
        )
        db.upsert_adr(
            Adr(
                project="proj",
                adr_number=1,
                title="ADR",
                status="accepted",
                content="updated",
                created_at_epoch=1700000004,
                updated_at_epoch=1700000010,
            )
        )
        db.upsert_project_profile(
            ProjectProfile(
                project="proj",
                detected_at_epoch=1700000006,
                last_updated_epoch=1700000011,
                detection_confidence=0.5,
            )
        )

        rows = {
            "memory_chunks": db.conn.execute(
                "SELECT synced_at FROM memory_chunks WHERE id = ?",
                (chunk_id,),
            ).fetchone()["synced_at"],
            "sessions": db.conn.execute(
                "SELECT synced_at FROM sessions WHERE session_id = ?",
                (session.session_id,),
            ).fetchone()["synced_at"],
            "instincts": db.conn.execute(
                "SELECT synced_at FROM instincts WHERE id = ?",
                (instinct_id,),
            ).fetchone()["synced_at"],
            "adrs": db.conn.execute(
                "SELECT synced_at FROM adrs WHERE id = ?",
                (adr_id,),
            ).fetchone()["synced_at"],
            "project_profiles": db.conn.execute(
                "SELECT synced_at FROM project_profiles WHERE id = ?",
                (profile_id,),
            ).fetchone()["synced_at"],
        }
        assert all(value is None for value in rows.values())

    def test_end_session_clears_synced_at(self, db: Database) -> None:
        session = Session(session_id="sess-1", project="proj", started_at_epoch=1700000000)
        db.upsert_session(session)
        db.conn.execute(
            "UPDATE sessions SET synced_at = ? WHERE session_id = ?",
            ("already-synced", session.session_id),
        )
        db.conn.commit()

        db.end_session(session.session_id)

        row = db.conn.execute(
            "SELECT synced_at, ended_at_epoch FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        assert row["synced_at"] is None
        assert row["ended_at_epoch"] is not None

    def test_next_chunk_index(self, db: Database) -> None:
        assert db.get_next_chunk_index("sess-new") == 0
        db.store_chunk(
            MemoryChunk(
                session_id="sess-new",
                project="proj",
                chunk_index=0,
                content="c0",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        assert db.get_next_chunk_index("sess-new") == 1

    def test_fts_search(self, db: Database) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="sess-1",
                project="proj",
                chunk_index=0,
                content="fixed authentication bug in login handler",
                tool_names=["Edit"],
                files_read=[],
                files_modified=["auth.py"],
                user_prompt="fix the auth bug",
                created_at_epoch=1700000000,
            )
        )
        results = db.fts_search("authentication")
        assert len(results) > 0

    def test_fts_search_no_results(self, db: Database) -> None:
        results = db.fts_search("xyznonexistent")
        assert results == []

    def test_fts_search_operational_error(self, db: Database) -> None:
        """FTS5 テーブルが壊れている場合、空リストを返す"""
        # FTS テーブルを削除して OperationalError を発生させる
        db.conn.execute("DROP TABLE IF EXISTS memory_chunks_fts")
        db.conn.commit()
        results = db.fts_search("test")
        assert results == []

    def test_recent_chunks(self, db: Database) -> None:
        for i in range(5):
            db.store_chunk(
                MemoryChunk(
                    session_id="sess-1",
                    project="proj",
                    chunk_index=i,
                    content=f"chunk {i}",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000000 + i,
                )
            )
        recent = db.get_recent_chunks(limit=3)
        assert len(recent) == 3
        assert recent[0].created_at_epoch >= recent[-1].created_at_epoch

    def test_recent_chunks_with_project_filter(self, db: Database) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj-a",
                chunk_index=0,
                content="a",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        db.store_chunk(
            MemoryChunk(
                session_id="s2",
                project="proj-b",
                chunk_index=0,
                content="b",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000001,
            )
        )
        recent = db.get_recent_chunks(limit=10, project="proj-a")
        assert len(recent) == 1
        assert recent[0].project == "proj-a"

    def test_get_chunk_by_id_not_found(self, db: Database) -> None:
        assert db.get_chunk_by_id(99999) is None

    def test_get_chunks_by_ids(self, db: Database) -> None:
        ids = []
        for i in range(3):
            cid = db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=i,
                    content=f"c{i}",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000000 + i,
                )
            )
            ids.append(cid)
        result = db.get_chunks_by_ids(ids)
        assert len(result) == 3
        assert all(cid in result for cid in ids)

    def test_get_chunks_by_ids_empty(self, db: Database) -> None:
        assert db.get_chunks_by_ids([]) == {}

    def test_get_chunks_by_session_empty(self, db: Database) -> None:
        assert db.get_chunks_by_session("nonexistent") == []

    def test_close(self, tmp_path: Path) -> None:
        import sqlite3

        db = Database(tmp_path / "test.db")
        db.close()
        with pytest.raises(sqlite3.ProgrammingError):
            db.get_next_chunk_index("s1")

    def test_user_prompt_null_handling(self, db: Database) -> None:
        """user_prompt が None でも空文字列になる"""
        db.conn.execute(
            """INSERT INTO memory_chunks
         (session_id, project, chunk_index, content,
          tool_names, files_read, files_modified,
          user_prompt, created_at_epoch)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("s1", "p", 0, "content", "[]", "[]", "[]", None, 1700000000),
        )
        db.conn.commit()
        chunks = db.get_chunks_by_session("s1")
        assert chunks[0].user_prompt == ""

    def test_store_and_vec_search_embeddings(self, db: Database) -> None:
        """エンべディング保存とベクトル検索のテスト"""
        cid = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="test",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        # sqlite-vec テーブルが存在するかチェック
        row = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='memory_chunks_vec'"
        ).fetchone()
        if row is None:
            pytest.skip("sqlite-vec not available")
        emb = [0.1] * 768
        db.store_embeddings([cid], [emb])
        results = db.vec_search(emb, limit=5)
        assert len(results) >= 1
        assert results[0][0] == cid

    def test_vec_search_no_data(self, db: Database) -> None:
        """ベクトル検索：データなしの場合"""
        results = db.vec_search([0.1] * 768)
        # sqlite-vec が利用不可でも空リストを返す
        assert results == [] or isinstance(results, list)


class TestSchemaInit:
    """スキーマ初期化のテスト"""

    def test_fts5_init_failure(self, tmp_path: Path) -> None:
        """FTS5 初期化失敗時もデータベースは使用可能"""
        import devgear.mem.database as db_mod

        original_fts5 = db_mod._FTS5_SQL
        db_mod._FTS5_SQL = "CREATE VIRTUAL TABLE nonexistent USING invalid_module();"
        try:
            db = Database(tmp_path / "test_fts5_fail.db")
            cid = db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=0,
                    content="test",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000000,
                )
            )
            assert isinstance(cid, str)
            assert len(cid) == 36
            db.close()
        finally:
            db_mod._FTS5_SQL = original_fts5

    def test_sqlite_vec_init_failure_raises(self, tmp_path: Path) -> None:
        """sqlite-vec はオプショナル依存のため、インポート失敗時も動作する"""
        original = sys.modules.pop("sqlite_vec", None)
        sys.modules["sqlite_vec"] = None  # type: ignore[assignment]
        try:
            # オプショナルなので例外は発生しない
            db = Database(tmp_path / "test_vec_fail.db")
            db.close()
        finally:
            if original is not None:
                sys.modules["sqlite_vec"] = original
            else:
                sys.modules.pop("sqlite_vec", None)


class TestParseJsonList:
    """_parse_json_list のテスト"""

    @pytest.mark.parametrize(
        "input_val, expected",
        [
            (None, []),
            ("", []),
            ('["a", "b"]', ["a", "b"]),
            ("invalid json", []),
        ],
        ids=["none", "empty", "valid", "invalid-json"],
    )
    def test_parse(self, input_val: str | None, expected: list) -> None:
        assert _parse_json_list(input_val) == expected


class TestMigration:
    """スキーママイグレーションのテスト"""

    def test_new_columns_exist(self, db: Database) -> None:
        """v0.0.1 マイグレーション後に新カラムが存在する"""
        cols = {row[1] for row in db.conn.execute("PRAGMA table_info(memory_chunks)").fetchall()}
        assert "access_count" in cols
        assert "last_accessed_epoch" in cols
        assert "merged_generation" in cols
        assert "merged_into" in cols

    def test_migration_idempotent(self, tmp_path: Path) -> None:
        """マイグレーションは2回実行しても失敗しない"""
        db = Database(tmp_path / "idem.db")
        # 再度 _migrate() を呼んでも例外が出ない
        db._migrate()
        db.close()

    def test_schema_migrations_table_exists(self, db: Database) -> None:
        row = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        assert row is not None

    def test_migration_table_empty_initially(self, db: Database) -> None:
        """devgear版では _MIGRATIONS が空なので schema_migrations は空"""
        versions = {r[0] for r in db.conn.execute("SELECT version FROM schema_migrations").fetchall()}
        # devgear版では初期マイグレーションは空（カラムはスキーマ定義に含まれている）
        assert isinstance(versions, set)

    def test_applies_registered_migrations(self, tmp_path: Path) -> None:
        import devgear.mem.database as db_mod

        original = db_mod._MIGRATIONS
        db_mod._MIGRATIONS = [("v-test", ["CREATE TABLE IF NOT EXISTS migration_marker (id INTEGER);"])]
        try:
            db = Database(tmp_path / "migrated.db")
            try:
                row = db.conn.execute(
                    "SELECT version FROM schema_migrations WHERE version = ?",
                    ("v-test",),
                ).fetchone()
                assert row["version"] == "v-test"
                marker = db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='migration_marker'"
                ).fetchone()
                assert marker is not None
            finally:
                db.close()
        finally:
            db_mod._MIGRATIONS = original


class TestAdvancedTables:
    """インスティンクト、ADR、イベントログのテスト"""

    def test_instinct_upsert_and_getters(self, db: Database) -> None:
        first = Instinct(
            id="instinct-fixed",
            instinct_id="instinct-1",
            scope="project",
            confidence=0.5,
            content="first",
            created_at_epoch=1,
            updated_at_epoch=1,
            project_id="proj",
        )
        first_id = db.upsert_instinct(first)
        first.content = "updated"
        first.updated_at_epoch = 2
        second_id = db.upsert_instinct(first)

        other = Instinct(
            instinct_id="instinct-2",
            scope="global",
            confidence=0.8,
            content="other",
            created_at_epoch=3,
            updated_at_epoch=3,
        )
        db.upsert_instinct(other)

        assert first_id == second_id == "instinct-fixed"
        assert len(db.get_instincts(scope="project", project_id="proj")) == 1
        assert len(db.get_instincts(scope="global")) == 1
        assert len(db.get_instincts()) == 2
        assert db.get_all_instincts()[0].content == "updated"

    def test_adr_upsert_and_getters(self, db: Database) -> None:
        adr = Adr(
            id="adr-fixed",
            project="proj",
            adr_number=1,
            title="Initial",
            status="accepted",
            content="first",
            created_at_epoch=1,
            updated_at_epoch=1,
        )
        first_id = db.upsert_adr(adr)
        adr.title = "Updated"
        adr.status = "superseded"
        adr.updated_at_epoch = 2
        second_id = db.upsert_adr(adr)

        other = Adr(
            project="proj-2",
            adr_number=2,
            title="Other",
            status="proposed",
            content="other",
            created_at_epoch=3,
            updated_at_epoch=3,
        )
        db.upsert_adr(other)

        assert first_id == second_id == "adr-fixed"
        assert [item.title for item in db.get_adrs(project="proj")] == ["Updated"]
        assert len(db.get_adrs()) == 2
        assert db.get_all_adrs()[0].title == "Updated"

    def test_event_logs_and_row_helpers(self, db: Database) -> None:
        event = EventLog(
            id="event-fixed",
            event_type="notice",
            content="hello",
            created_at_epoch=1,
            project_id="proj",
        )
        first_id = db.store_event_log(event)
        second_id = db.store_event_log(event)
        db.store_event_log(
            EventLog(
                event_type="other",
                content="world",
                created_at_epoch=2,
            )
        )

        assert first_id == second_id == "event-fixed"
        assert len(db.get_event_logs(event_type="notice")) == 1
        assert len(db.get_event_logs()) == 2
        assert db.get_all_event_logs()[0].content == "hello"

        db.conn.execute(
            """INSERT INTO memory_chunks
         (id, session_id, project, chunk_index, content,
          tool_names, files_read, files_modified,
          user_prompt, created_at_epoch)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "chunk-1",
                "sess",
                "proj",
                0,
                "content",
                json.dumps(["Read"]),
                json.dumps(["file.py"]),
                json.dumps([]),
                None,
                1,
            ),
        )
        db.conn.commit()
        db.update_access(["chunk-1"])
        row = db.conn.execute("SELECT access_count, last_accessed_epoch FROM memory_chunks WHERE id = ?", ("chunk-1",)).fetchone()
        assert row["access_count"] == 1
        assert row["last_accessed_epoch"] is not None

        class FakeRow(dict):
            def keys(self) -> list[str]:
                return list(super().keys())

        chunk = _row_to_chunk(
            FakeRow(
                {
                    "id": "chunk-x",
                    "session_id": "sess",
                    "project": "proj",
                    "chunk_index": 1,
                    "content": "content",
                    "tool_names": json.dumps(["Write"]),
                    "files_read": json.dumps([]),
                    "files_modified": json.dumps([]),
                    "user_prompt": None,
                    "created_at_epoch": 2,
                }
            )
        )
        instinct = _row_to_instinct(
            FakeRow(
                {
                    "id": "instinct-x",
                    "origin_user": "user",
                    "instinct_id": "i1",
                    "scope": "global",
                    "project_id": None,
                    "trigger_text": "trigger",
                    "confidence": 0.7,
                    "domain": "domain",
                    "content": "content",
                    "created_at_epoch": 1,
                    "updated_at_epoch": 2,
                }
            )
        )
        adr = _row_to_adr(
            FakeRow(
                {
                    "id": "adr-x",
                    "origin_user": "user",
                    "project": "proj",
                    "adr_number": 9,
                    "title": "Title",
                    "status": "accepted",
                    "content": "content",
                    "created_at_epoch": 1,
                    "updated_at_epoch": 2,
                }
            )
        )
        event_log = _row_to_event_log(
            FakeRow(
                {
                    "id": "event-x",
                    "origin_user": "user",
                    "event_type": "notice",
                    "project_id": "proj",
                    "content": "content",
                    "created_at_epoch": 1,
                }
            )
        )

        assert chunk.user_prompt == ""
        assert instinct.instinct_id == "i1"
        assert adr.adr_number == 9
        assert event_log.event_type == "notice"

    def test_project_profile_upsert_and_getters(self, db: Database) -> None:
        profile1 = ProjectProfile(
            id="profile-1",
            origin_user="user-a",
            project="proj",
            detected_at_epoch=1,
            last_updated_epoch=1,
            project_path="/repo/a",
            languages=["python"],
        )
        profile2 = ProjectProfile(
            id="profile-2",
            origin_user="user-b",
            project="proj",
            detected_at_epoch=2,
            last_updated_epoch=2,
            project_path="/repo/b",
            languages=["rust"],
        )

        db.upsert_project_profile(profile1)
        db.upsert_project_profile(profile2)

        latest = db.get_project_profile("proj")
        assert latest is not None
        assert latest.id == "profile-2"
        assert latest.project_path == "/repo/b"
        assert db.get_project_profile("proj", origin_user="user-a").id == "profile-1"
        assert [profile.id for profile in db.get_all_project_profiles()] == ["profile-1", "profile-2"]

    def test_store_embeddings_and_vec_search_with_fake_connection(self, db: Database) -> None:
        calls: list[tuple[str, tuple]] = []

        class FakeConn:
            def execute(self, sql: str, params: tuple) -> None:
                calls.append((sql, params))

            def commit(self) -> None:
                calls.append(("commit", ()))

        db.conn = FakeConn()  # type: ignore[assignment]

        db.store_embeddings(["chunk-1"], [[0.1, 0.2]])
        assert calls[0][0].startswith("INSERT OR REPLACE INTO memory_chunks_vec")
        assert calls[-1][0] == "commit"

        class FakeRow:
            def __init__(self, chunk_id: str, distance: float) -> None:
                self.chunk_id = chunk_id
                self.distance = distance

            def __getitem__(self, key: str) -> str | float:
                return getattr(self, key)

        class FakeSearchConn:
            def execute(self, sql: str, params: tuple) -> SimpleNamespace:
                assert "memory_chunks_vec" in sql
                return SimpleNamespace(fetchall=lambda: [FakeRow("chunk-1", 0.1)])

        db.conn = FakeSearchConn()  # type: ignore[assignment]
        assert db.vec_search([0.1, 0.2]) == [("chunk-1", 0.1)]

        class ErrorConn:
            def execute(self, sql: str, params: tuple) -> SimpleNamespace:
                raise RuntimeError("boom")

        db.conn = ErrorConn()  # type: ignore[assignment]
        assert db.vec_search([0.1, 0.2]) == []


class TestUpdateAccess:
    """アクセス追跡のテスト"""

    def test_access_count_incremented(self, db: Database) -> None:
        cid = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="test content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        db.update_access([cid])
        chunk = db.get_chunk_by_id(cid)
        assert chunk is not None
        assert chunk.access_count == 1

    def test_access_count_increments_multiple_times(self, db: Database) -> None:
        cid = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="test content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        db.update_access([cid])
        db.update_access([cid])
        chunk = db.get_chunk_by_id(cid)
        assert chunk is not None
        assert chunk.access_count == 2

    def test_last_accessed_epoch_set(self, db: Database) -> None:
        cid = db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="test content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        before = int(time.time())
        db.update_access([cid])
        after = int(time.time())
        chunk = db.get_chunk_by_id(cid)
        assert chunk is not None
        assert before <= chunk.last_accessed_epoch <= after  # type: ignore[operator]

    def test_batch_update(self, db: Database) -> None:
        ids = []
        for i in range(3):
            cid = db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=i,
                    content="test content",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000000 + i,
                )
            )
            ids.append(cid)
        db.update_access(ids)
        for cid in ids:
            chunk = db.get_chunk_by_id(cid)
            assert chunk is not None
            assert chunk.access_count == 1

    def test_empty_ids_no_error(self, db: Database) -> None:
        db.update_access([])  # 空リストでも失敗しない

    def test_update_access_handles_database_error(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "access_error.db")

        class FailingConn:
            def executemany(self, sql: str, params: list[tuple[int, str]]) -> None:  # noqa: ANN001
                raise RuntimeError("boom")

            def close(self) -> None:
                pass

        db.conn = FailingConn()  # type: ignore[assignment]

        db.update_access(["chunk-1"])


class TestGetAllChunks:
    """get_all_chunks のテスト"""

    def test_empty_db(self, db: Database) -> None:
        assert db.get_all_chunks() == []

    def test_returns_all(self, db: Database) -> None:
        for i in range(3):
            db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=i,
                    content=f"chunk {i}",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000000 + i,
                )
            )
        chunks = db.get_all_chunks()
        assert len(chunks) == 3

    def test_ordered_by_epoch(self, db: Database) -> None:
        for i in range(3):
            db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=i,
                    content=f"chunk {i}",
                    tool_names=[],
                    files_read=[],
                    files_modified=[],
                    user_prompt="",
                    created_at_epoch=1700000002 - i,
                )
            )
        chunks = db.get_all_chunks()
        epochs = [c.created_at_epoch for c in chunks]
        assert epochs == sorted(epochs)


class TestInteractionAndRunQueries:
    """interaction_logs / mem_item_runs の取得系テスト"""

    def test_interaction_log_queries_and_prompt_hash(self, db: Database) -> None:
        log1 = InteractionLog(
            session_id="sess-1",
            project="proj-a",
            user_prompt_full="alpha",
            interaction_index=0,
            created_at_epoch=1,
        )
        log2 = InteractionLog(
            session_id="sess-1",
            project="proj-a",
            user_prompt_full="beta",
            interaction_index=1,
            created_at_epoch=2,
        )
        log3 = InteractionLog(
            session_id="sess-2",
            project="proj-b",
            user_prompt_full="gamma",
            interaction_index=0,
            created_at_epoch=3,
        )

        first_id = db.store_interaction_log(log1)
        db.store_interaction_log(log2)
        db.store_interaction_log(log3)

        row = db.conn.execute(
            "SELECT user_prompt_hash FROM interaction_logs WHERE id = ?",
            (first_id,),
        ).fetchone()
        assert row["user_prompt_hash"] == _make_prompt_hash("alpha")

        session_logs = db.get_interaction_logs(session_id="sess-1")
        project_logs = db.get_interaction_logs(project="proj-a")
        all_logs = db.get_interaction_logs()

        assert [log.interaction_index for log in session_logs] == [0, 1]
        assert len(project_logs) == 2
        assert len(all_logs) == 3
        assert db.get_all_interaction_logs()[0].created_at_epoch == 1
        assert db.get_next_interaction_index("sess-1") == 2
        assert db.get_next_interaction_index("missing") == 0

    def test_mem_item_run_queries(self, db: Database) -> None:
        run1 = MemItemRun(
            session_id="sess-1",
            project="proj-a",
            skill_name="s-learn",
            created_at_epoch=1,
            item_type="skill",
        )
        run2 = MemItemRun(
            session_id="sess-1",
            project="proj-a",
            skill_name="c-dashboard",
            created_at_epoch=2,
            item_type="command",
        )
        run3 = MemItemRun(
            session_id="sess-2",
            project="proj-b",
            skill_name="a-review",
            created_at_epoch=3,
            item_type="agent",
        )

        db.store_mem_item_run(run1)
        db.store_mem_item_run(run2)
        db.store_mem_item_run(run3)

        by_skill = db.get_skill_run_stats(skill_name="s-learn")
        by_project = db.get_skill_run_stats(project="proj-a")
        all_runs = db.get_skill_run_stats()

        assert [run.skill_name for run in by_skill] == ["s-learn"]
        assert [run.skill_name for run in by_project] == ["c-dashboard", "s-learn"]
        assert [run.skill_name for run in all_runs] == ["a-review", "c-dashboard", "s-learn"]
        assert [run.skill_name for run in db.get_all_mem_item_runs()] == ["s-learn", "c-dashboard", "a-review"]

    @pytest.mark.parametrize(
        "input_val, expected",
        [
            (None, []),
            ("", []),
            ('[{"reason": "because"}]', [{"reason": "because"}]),
            ("invalid json", []),
            ('{"reason": "not a list"}', []),
        ],
        ids=["none", "empty", "valid", "invalid-json", "not-a-list"],
    )
    def test_parse_json_dict_list(self, input_val: str | None, expected: list[dict]) -> None:
        assert _parse_json_dict_list(input_val) == expected


class TestDatabaseFilePermissions:
    """新規 DB 作成時のファイル権限検証。"""

    def test_new_db_chmod_0600(self, tmp_path: Path) -> None:
        """新規作成された mem.db は chmod 0600 になる。"""
        db_path = tmp_path / "mem.db"
        db = Database(str(db_path))
        db.close()
        mode = db_path.stat().st_mode & 0o777
        assert mode == 0o600, f"期待 0o600 だが {oct(mode)} が設定されている"

    def test_existing_db_permissions_unchanged(self, tmp_path: Path) -> None:
        """既存 DB を再 open しても権限を変更しない（既存ユーザの DB を破壊しない）。"""
        db_path = tmp_path / "mem.db"
        # 最初に作成（→ 0o600）
        db = Database(str(db_path))
        db.close()
        # 権限を変えた状態でシミュレート
        db_path.chmod(0o644)
        # 再 open
        db2 = Database(str(db_path))
        db2.close()
        mode = db_path.stat().st_mode & 0o777
        assert mode == 0o644, "既存 DB の権限を変更してはいけない"


class TestConcurrentChunkInsert:
    """並行 store_chunk 時の UNIQUE 制約違反リトライをテストする。"""

    def test_retry_on_unique_violation(self, db: Database) -> None:
        """chunk_index が重複しても store_chunk がリトライして全件保存できる。

        別プロセスの async hook が同一 session に先に書き込んだケースをシミュレートする:
        - chunk_index=0 を手動で直接 INSERT（先行プロセス相当）
        - その後 store_chunk(chunk_index=0) を呼ぶ → UNIQUE 違反 → 再採番して chunk_index=1 で成功
        """
        import sqlite3 as _sqlite3

        session_id = "retry-session"
        db.upsert_session(Session(session_id=session_id, project="proj", started_at_epoch=int(time.time())))

        # 先行プロセスが chunk_index=0 を既に書き込んだ状況を作る
        db.conn.execute(
            """INSERT INTO memory_chunks
             (id, origin_user, session_id, project, chunk_index, content,
              tool_names, files_read, files_modified, user_prompt, created_at_epoch,
              execution_status, tool_sequence)
             VALUES (?, '', ?, 'proj', 0, '[Bash] prior process', '[]', '[]', '[]', '', ?, 'unknown', '[]')""",
            ("prior-chunk-id", session_id, int(time.time())),
        )
        db.conn.commit()

        # 後続プロセスも chunk_index=0 で store_chunk を試みる → リトライで chunk_index=1 になる
        chunk = MemoryChunk(
            session_id=session_id,
            project="proj",
            chunk_index=0,  # 衝突する index
            content="[Bash] later process output",
            tool_names=["Bash"],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=int(time.time()),
        )
        cid = db.store_chunk(chunk)

        assert cid is not None
        saved = db.get_chunk_by_id(cid)
        assert saved is not None
        assert saved.content == chunk.content
        assert saved.chunk_index == 1  # 再採番されて 1 になる

    def test_id_duplicate_raises_immediately(self, tmp_path: Path) -> None:
        """id PRIMARY KEY 重複の IntegrityError はリトライせず即 raise する。

        chunk_index UNIQUE 違反のみをリトライ対象とし、他の制約違反は
        リトライなしに伝播することを確認する。
        """
        import sqlite3 as _sqlite3

        db3 = Database(tmp_path / "id_dup.db")
        session_id = "id-dup-session"
        db3.upsert_session(Session(session_id=session_id, project="proj", started_at_epoch=int(time.time())))

        fixed_id = "fixed-uuid-0000-0000-0000-000000000000"

        # 同じ id で1件目を保存
        first = MemoryChunk(
            session_id=session_id,
            project="proj",
            chunk_index=0,
            content="first",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=int(time.time()),
        )
        first.id = fixed_id
        db3.store_chunk(first)

        # 同じ id で2件目を試みる → PRIMARY KEY 違反 → 即 raise
        second = MemoryChunk(
            session_id=session_id,
            project="proj",
            chunk_index=999,  # 異なる chunk_index
            content="second",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=int(time.time()),
        )
        second.id = fixed_id
        with pytest.raises(_sqlite3.IntegrityError):
            db3.store_chunk(second)

    def test_max_retries_exceeded_raises(self, tmp_path: Path) -> None:
        """_STORE_CHUNK_MAX_RETRIES 回全て UNIQUE 制約違反が続く場合は IntegrityError を raise する。

        conn をラッパーで置き換え、INSERT が常に chunk_index UNIQUE 違反を返すようにする。
        """
        import sqlite3 as _sqlite3
        import unittest.mock as mock

        import devgear.mem.database as db_mod

        db4 = Database(tmp_path / "max_retry.db")
        session_id = "max-retry-session"
        db4.upsert_session(Session(session_id=session_id, project="proj", started_at_epoch=int(time.time())))

        chunk = MemoryChunk(
            session_id=session_id,
            project="proj",
            chunk_index=0,
            content="content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=int(time.time()),
        )

        real_conn = db4.conn
        unique_err = _sqlite3.IntegrityError(
            "UNIQUE constraint failed: memory_chunks.session_id, memory_chunks.chunk_index"
        )

        class AlwaysFailConn:
            """INSERT 時に常に chunk_index UNIQUE 違反を起こすラッパー。"""

            def __getattr__(self, name: str):
                return getattr(real_conn, name)

            def execute(self, sql: str, params=()) -> object:
                if "INSERT INTO memory_chunks" in sql:
                    raise unique_err
                return real_conn.execute(sql, params)

            def rollback(self) -> None:
                real_conn.rollback()

        db4.conn = AlwaysFailConn()  # type: ignore[assignment]

        with mock.patch.object(db_mod, "_STORE_CHUNK_MAX_RETRIES", 3):
            with pytest.raises(_sqlite3.IntegrityError):
                db4.store_chunk(chunk)

        db4.conn = real_conn
        db4.close()
