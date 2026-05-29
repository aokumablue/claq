"""devgear.mem.pg_database の追加テスト。"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest

from devgear.mem.database import (
    Adr,
    EventLog,
    Instinct,
    InteractionLog,
    MemItemRun,
    MemoryChunk,
    ProjectProfile,
    Session,
)
from devgear.mem.pg_database import PgDatabase, _ensure_ssl, _to_json


class FakeCursor:
    def __init__(self, *, fetchone_result=None, fetchall_result=None) -> None:  # noqa: ANN001
        self.fetchone_result = fetchone_result
        self.fetchall_result = fetchall_result or []
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:  # noqa: ANN001
        return False

    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
        self.executed.append((sql, params))

    def executemany(self, sql: str, params_list) -> None:  # noqa: ANN001
        for params in params_list:
            self.executed.append((sql, params))

    def fetchone(self):  # noqa: ANN001
        return self.fetchone_result

    def fetchall(self):  # noqa: ANN001
        return self.fetchall_result


class FakeConn:
    def __init__(self, cursor: FakeCursor | None = None) -> None:
        self.cursor_obj = cursor or FakeCursor()
        self.closed = False
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    def cursor(self) -> FakeCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commit_calls += 1

    def rollback(self) -> None:
        self.rollback_calls += 1

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True


class BoomCursor(FakeCursor):
    def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
        raise RuntimeError("boom")

    def executemany(self, sql: str, params_list) -> None:  # noqa: ANN001
        raise RuntimeError("boom")


class FakePool:
    def __init__(self, url: str, min_size: int, max_size: int, conn: FakeConn) -> None:
        self.url = url
        self.min_size = min_size
        self.max_size = max_size
        self.conn = conn
        self.getconn_calls = 0
        self.putconn_calls: list[FakeConn] = []
        self.closed = False

    def getconn(self) -> FakeConn:
        self.getconn_calls += 1
        return self.conn

    def putconn(self, conn: FakeConn) -> None:
        self.putconn_calls.append(conn)

    def close(self) -> None:
        self.closed = True


def _make_chunk() -> MemoryChunk:
    return MemoryChunk(
        id="chunk-1",
        session_id="sess-1",
        project="proj",
        chunk_index=1,
        content="content",
        tool_names=["Edit"],
        files_read=["src/app.py"],
        files_modified=["src/app.py"],
        user_prompt="prompt",
        created_at_epoch=1700000000,
    )


def _make_session() -> Session:
    return Session(
        id="session-1",
        session_id="sess-1",
        project="proj",
        started_at_epoch=1700000000,
        chunk_count=2,
    )


def _make_instinct() -> Instinct:
    return Instinct(
        id="inst-1",
        instinct_id="inst-1",
        scope="project",
        confidence=0.8,
        content="content",
        created_at_epoch=1700000000,
        updated_at_epoch=1700000100,
        origin_user="user",
        project_id="proj",
        trigger_text="when testing",
        domain="testing",
    )


def _make_adr() -> Adr:
    return Adr(
        id="adr-1",
        project="proj",
        adr_number=1,
        title="title",
        status="accepted",
        content="content",
        created_at_epoch=1700000000,
        updated_at_epoch=1700000100,
        origin_user="user",
    )


def _make_event() -> EventLog:
    return EventLog(
        id="event-1",
        event_type="custom",
        content="content",
        created_at_epoch=1700000000,
        origin_user="user",
        project_id="proj",
    )


def _make_interaction_log() -> InteractionLog:
    return InteractionLog(
        id="ilog-1",
        session_id="sess-1",
        project="proj",
        user_prompt_full="do something",
        interaction_index=0,
        created_at_epoch=1700000000,
        origin_user="user",
        user_prompt_hash="abcd1234",
        ai_response_summary="summary",
        execution_outcome="success",
        tool_error_count=0,
    )


def _make_project_profile() -> ProjectProfile:
    return ProjectProfile(
        id="profile-1",
        project="proj",
        detected_at_epoch=1700000000,
        last_updated_epoch=1700000100,
        origin_user="user",
        project_path="/path/to/proj",
        languages=["python"],
        frameworks=["pytest"],
        primary_language="python",
        detection_confidence=0.95,
    )


def _make_skill_run() -> MemItemRun:
    return MemItemRun(
        id="run-1",
        session_id="sess-1",
        project="proj",
        skill_name="s-tdd",
        created_at_epoch=1700000000,
        origin_user="user",
        outcome="success",
        tools_used=["Edit", "Bash"],
        files_modified_count=2,
    )


def test_to_json_and_get_conn_modes(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _to_json(None) is None
    assert _to_json(["a", "b"]) == '["a", "b"]'
    assert _to_json({"k": "v"}) == '{"k": "v"}'

    pool_conn = FakeConn()
    pool_mod = ModuleType("psycopg_pool")

    class ConnectionPool:
        def __init__(self, url: str, min_size: int, max_size: int) -> None:
            self.url = url
            self.min_size = min_size
            self.max_size = max_size
            self.getconn_calls = 0

        def getconn(self) -> FakeConn:
            self.getconn_calls += 1
            return pool_conn

        def putconn(self, conn: FakeConn) -> None:  # noqa: ANN001
            self.putconn_calls = conn

        def close(self) -> None:
            self.closed = True

    pool_mod.ConnectionPool = ConnectionPool
    monkeypatch.setitem(sys.modules, "psycopg_pool", pool_mod)
    monkeypatch.setitem(sys.modules, "psycopg", ModuleType("psycopg"))

    db_pool = PgDatabase("postgres://example", use_pool=True)
    assert db_pool._get_conn() is pool_conn

    fallback_conn = FakeConn()
    psycopg_mod = ModuleType("psycopg")
    psycopg_mod.connect = lambda url: fallback_conn  # type: ignore[assignment]
    monkeypatch.setitem(sys.modules, "psycopg", psycopg_mod)
    monkeypatch.setitem(sys.modules, "psycopg_pool", ModuleType("psycopg_pool"))

    db_fallback = PgDatabase("postgres://fallback", use_pool=True)
    assert db_fallback._get_conn() is fallback_conn
    assert db_fallback._use_pool is False


def test_transaction_close_and_test_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    conn = FakeConn()
    db = PgDatabase("postgres://example", use_pool=False)
    put_calls: list[FakeConn] = []
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    monkeypatch.setattr(db, "_put_conn", lambda current: put_calls.append(current))

    with db.transaction():
        pass
    assert conn.commit_calls == 1
    assert put_calls == [conn]

    with pytest.raises(RuntimeError):
        with db.transaction():
            raise RuntimeError("boom")
    assert conn.rollback_calls == 1

    success_cursor = FakeCursor(fetchone_result=(1,))
    success_conn = FakeConn(success_cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    put_calls.clear()
    monkeypatch.setattr(db, "_get_conn", lambda: success_conn)
    monkeypatch.setattr(db, "_put_conn", lambda current: put_calls.append(current))
    assert db.test_connection() is True
    assert put_calls == [success_conn]

    class ErrorCursor(FakeCursor):
        def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
            raise RuntimeError("boom")

    error_conn = FakeConn(ErrorCursor())
    db = PgDatabase("postgres://example", use_pool=False)
    put_calls.clear()
    monkeypatch.setattr(db, "_get_conn", lambda: error_conn)
    monkeypatch.setattr(db, "_put_conn", lambda current: put_calls.append(current))
    assert db.test_connection() is False
    assert put_calls == [error_conn]

    pool = FakePool("postgres://example", 1, 4, conn)
    db = PgDatabase("postgres://example", use_pool=True)
    db._pool = pool
    db._conn = conn
    db.close()
    assert pool.closed is True
    assert conn.closed is True


def test_put_conn_returns_to_pool() -> None:
    conn = FakeConn()
    pool = FakePool("postgres://example", 1, 4, conn)
    db = PgDatabase("postgres://example", use_pool=True)
    db._pool = pool
    db._put_conn(conn)
    assert pool.putconn_calls == [conn]


def test_upsert_and_batch_methods(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    chunk = _make_chunk()
    session = _make_session()
    instinct = _make_instinct()
    adr = _make_adr()
    event = _make_event()

    assert db.upsert_chunks_batch([], "user") == 0
    assert db.upsert_sessions_batch([], "user") == 0
    assert db.upsert_instincts_batch([]) == 0
    assert db.upsert_adrs_batch([]) == 0
    assert db.insert_event_logs_batch([]) == 0

    db.upsert_chunk(chunk, "user")
    db.upsert_session(session, "user")
    db.upsert_instinct(instinct)
    db.upsert_adr(adr)
    db.insert_event_log(event)

    assert db.upsert_chunks_batch([chunk], "user") == 1
    assert db.upsert_sessions_batch([session], "user") == 1
    assert db.upsert_instincts_batch([instinct]) == 1
    assert db.upsert_adrs_batch([adr]) == 1
    assert db.insert_event_logs_batch([event]) == 1
    assert conn.commit_calls == 10
    assert any("memory_chunks" in sql for sql, _ in cursor.executed)
    assert any("sessions" in sql for sql, _ in cursor.executed)
    assert any("instincts" in sql for sql, _ in cursor.executed)
    assert any("adrs" in sql for sql, _ in cursor.executed)
    assert any("event_logs" in sql for sql, _ in cursor.executed)


def test_upsert_session_includes_git_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    """sessions upsert が git メタデータを含むことを確認する。"""
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    session = Session(
        id="session-2",
        session_id="sess-2",
        project="proj",
        started_at_epoch=1700000000,
        chunk_count=3,
        branch="main",
        commit_hash="abc1234",
        uncommitted_count=2,
        ended_at_epoch=1700001000,
        project_profile_id="profile-1",
    )
    db.upsert_session(session, "user")

    assert len(cursor.executed) == 1
    sql, params = cursor.executed[0]
    assert "branch" in sql
    assert "commit_hash" in sql
    assert "uncommitted_count" in sql
    assert "ended_at_epoch" in sql
    assert "project_profile_id" in sql
    assert params is not None
    assert "main" in params
    assert "abc1234" in params
    assert 2 in params
    assert 1700001000 in params
    assert "profile-1" in params

    # batch 版も同様のカラムを含む
    cursor.executed.clear()
    assert db.upsert_sessions_batch([session], "user") == 1
    batch_sql, batch_params = cursor.executed[0]
    assert "branch" in batch_sql
    assert "ended_at_epoch" in batch_sql
    assert batch_params is not None
    assert "main" in batch_params


def test_upsert_interaction_logs_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """interaction_logs バッチ upsert のテスト。"""
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert db.upsert_interaction_logs_batch([]) == 0

    log = _make_interaction_log()
    assert db.upsert_interaction_logs_batch([log]) == 1
    assert conn.commit_calls == 1
    assert any("interaction_logs" in sql for sql, _ in cursor.executed)

    sql, params = cursor.executed[0]
    assert "user_prompt_full" in sql
    assert "execution_outcome" in sql
    assert params is not None
    assert "do something" in params
    assert "success" in params


def test_upsert_project_profiles_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """project_profiles バッチ upsert のテスト。"""
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert db.upsert_project_profiles_batch([]) == 0

    profile = _make_project_profile()
    assert db.upsert_project_profiles_batch([profile]) == 1
    assert conn.commit_calls == 1
    assert any("project_profiles" in sql for sql, _ in cursor.executed)

    sql, params = cursor.executed[0]
    assert "origin_user" in sql
    assert "primary_language" in sql
    assert "detection_confidence" in sql
    assert params is not None
    assert "user" in params
    assert "python" in params
    assert "/path/to/proj" in params


def test_upsert_mem_item_runs_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    """mem_item_runs バッチ upsert のテスト。"""
    cursor = FakeCursor()
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert db.upsert_mem_item_runs_batch([]) == 0

    run = _make_skill_run()
    assert db.upsert_mem_item_runs_batch([run]) == 1
    assert conn.commit_calls == 1
    assert any("mem_item_runs" in sql for sql, _ in cursor.executed)

    sql, params = cursor.executed[0]
    assert "skill_name" in sql
    assert "outcome" in sql
    assert "files_modified_count" in sql
    assert params is not None
    assert "user" in params
    assert "s-tdd" in params
    assert "success" in params


def test_embeddings_search_and_team_search(monkeypatch: pytest.MonkeyPatch) -> None:
    embeddings_cursor = FakeCursor()
    embeddings_conn = FakeConn(embeddings_cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    put_calls: list[FakeConn] = []
    monkeypatch.setattr(db, "_get_conn", lambda: embeddings_conn)
    monkeypatch.setattr(db, "_put_conn", lambda current: put_calls.append(current))

    assert db.upsert_embeddings_batch([]) == 0
    assert db.upsert_embeddings_batch([("chunk-1", [0.1, 0.2])]) == 1
    assert embeddings_conn.commit_calls == 1
    assert put_calls == [embeddings_conn]
    assert any("[0.1,0.2]" in str(params) for _sql, params in embeddings_cursor.executed)

    vec_cursor = FakeCursor(fetchall_result=[("chunk-1", 0.1), ("chunk-2", 0.2)])
    vec_conn = FakeConn(vec_cursor)
    monkeypatch.setattr(db, "_get_conn", lambda: vec_conn)
    assert db.vec_search([0.1, 0.2], limit=2) == [("chunk-1", 0.1), ("chunk-2", 0.2)]
    assert "embedding <->" in vec_cursor.executed[0][0]

    fts_cursor = FakeCursor(fetchall_result=[("chunk-3", 0.9)])
    fts_conn = FakeConn(fts_cursor)
    monkeypatch.setattr(db, "_get_conn", lambda: fts_conn)
    assert db.fts_search("hello", limit=1) == [("chunk-3", 0.9)]
    assert "similarity(content" in fts_cursor.executed[0][0]

    monkeypatch.setattr(
        db,
        "fts_search",
        lambda query, limit=20, *, exclude_origin_user=None: [("a", 0.9), ("b", 0.8)],
    )
    monkeypatch.setattr(
        db,
        "vec_search",
        lambda embedding, limit=20, *, exclude_origin_user=None: [("b", 0.7), ("c", 0.6)],
    )
    ranked = db.team_search("query", [0.1, 0.2], limit=3)
    assert [row[0] for row in ranked] == ["b", "a", "c"]


def test_search_with_origin_user_exclusion(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(fetchall_result=[("chunk-1", 0.1)])
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    assert db.vec_search([0.1, 0.2], limit=1, exclude_origin_user="me") == [("chunk-1", 0.1)]
    assert "origin_user <>" in cursor.executed[0][0]

    cursor = FakeCursor(fetchall_result=[("chunk-2", 0.9)])
    conn = FakeConn(cursor)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    assert db.fts_search("hello", limit=1, exclude_origin_user="me") == [("chunk-2", 0.9)]
    assert "origin_user <>" in cursor.executed[0][0]


def test_query_methods_raise_and_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    cases = [
        lambda db: db.upsert_chunk(_make_chunk(), "user"),
        lambda db: db.upsert_chunks_batch([_make_chunk()], "user"),
        lambda db: db.upsert_session(_make_session(), "user"),
        lambda db: db.upsert_sessions_batch([_make_session()], "user"),
        lambda db: db.upsert_instinct(_make_instinct()),
        lambda db: db.upsert_instincts_batch([_make_instinct()]),
        lambda db: db.upsert_adr(_make_adr()),
        lambda db: db.upsert_adrs_batch([_make_adr()]),
        lambda db: db.insert_event_log(_make_event()),
        lambda db: db.insert_event_logs_batch([_make_event()]),
        lambda db: db.upsert_embeddings_batch([("chunk-1", [0.1, 0.2])]),
        lambda db: db.upsert_interaction_logs_batch([_make_interaction_log()]),
        lambda db: db.upsert_project_profiles_batch([_make_project_profile()]),
        lambda db: db.upsert_mem_item_runs_batch([_make_skill_run()]),
    ]

    for call in cases:
        cursor = BoomCursor()
        conn = FakeConn(cursor)
        db = PgDatabase("postgres://example", use_pool=False)
        monkeypatch.setattr(db, "_get_conn", lambda conn=conn: conn)
        with pytest.raises(RuntimeError):
            call(db)
        assert conn.rollback_calls == 1


def test_fetch_chunks_by_ids_parses_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    cursor = FakeCursor(
        fetchall_result=[
            (
                "chunk-1",
                "origin",
                "content",
                "prompt",
                "proj",
                1700000000,
                ["Edit"],
                "[\"file.py\"]",
                "not-json",
            )
        ]
    )
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)

    rows = db.fetch_chunks_by_ids(["chunk-1"])
    assert rows["chunk-1"]["tool_names"] == ["Edit"]
    assert rows["chunk-1"]["files_read"] == ["file.py"]
    assert rows["chunk-1"]["files_modified"] == []


class TestEnsureSsl:
    """_ensure_ssl のテーブル駆動テスト。"""

    def test_no_sslmode_adds_require(self) -> None:
        """sslmode 未指定 URL には sslmode=require が自動付与される。"""
        result = _ensure_ssl("postgresql://user@host/db")
        assert "sslmode=require" in result

    def test_require_unchanged(self) -> None:
        """sslmode=require はそのまま維持される。"""
        url = "postgresql://user@host/db?sslmode=require"
        assert _ensure_ssl(url) == url

    def test_verify_full_unchanged(self) -> None:
        """sslmode=verify-full はそのまま維持される。"""
        url = "postgresql://user@host/db?sslmode=verify-full"
        assert _ensure_ssl(url) == url

    def test_disable_raises(self) -> None:
        """sslmode=disable は ValueError を発生させる（フェイルクローズ）。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@host/db?sslmode=disable")

    def test_allow_raises(self) -> None:
        """sslmode=allow は ValueError を発生させる。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@host/db?sslmode=allow")

    def test_prefer_raises(self) -> None:
        """sslmode=prefer は ValueError を発生させる。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@host/db?sslmode=prefer")

    def test_url_with_other_params_preserves_them(self) -> None:
        """他のクエリパラメータは保持される。"""
        result = _ensure_ssl("postgresql://user@host/db?application_name=test")
        assert "sslmode=require" in result
        assert "application_name=test" in result

    # --- ローカルホスト例外 ---

    def test_localhost_disable_allowed(self) -> None:
        """localhost では sslmode=disable が許可される（SSL 非対応 PG 向け）。"""
        url = "postgresql://user@localhost/db?sslmode=disable"
        result = _ensure_ssl(url)
        assert "sslmode=disable" in result

    def test_127_0_0_1_disable_allowed(self) -> None:
        """127.0.0.1 では sslmode=disable が許可される。"""
        url = "postgresql://user@127.0.0.1/db?sslmode=disable"
        result = _ensure_ssl(url)
        assert "sslmode=disable" in result

    def test_ipv6_loopback_disable_allowed(self) -> None:
        """IPv6 ループバック (::1) では sslmode=disable が許可される。"""
        url = "postgresql://user@[::1]/db?sslmode=disable"
        result = _ensure_ssl(url)
        assert "sslmode=disable" in result

    def test_localhost_no_sslmode_adds_require(self) -> None:
        """localhost でも sslmode 未指定なら sslmode=require を付与する。"""
        result = _ensure_ssl("postgresql://user@localhost/db")
        assert "sslmode=require" in result

    def test_remote_host_disable_still_raises(self) -> None:
        """リモートホストでは sslmode=disable は依然として ValueError。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@remote.example.com/db?sslmode=disable")

    def test_localhost_allow_still_raises(self) -> None:
        """localhost でも sslmode=allow は許可しない（disable のみ例外）。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@localhost/db?sslmode=allow")

    def test_localhost_prefer_still_raises(self) -> None:
        """localhost でも sslmode=prefer は許可しない（disable のみ例外）。"""
        with pytest.raises(ValueError, match="sslmode"):
            _ensure_ssl("postgresql://user@localhost/db?sslmode=prefer")


class TestTestConnectionProbeCache:
    """test_connection の TTL キャッシュ動作のテスト。"""

    def _make_error_cursor(self) -> FakeCursor:
        class _ErrorCursor(FakeCursor):
            def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
                raise RuntimeError("connection refused")

        return _ErrorCursor()

    def test_failure_is_cached_within_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """失敗時は TTL 内に再試行せずキャッシュ値を返す。"""
        call_count = 0

        class CountingCursor(FakeCursor):
            def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
                nonlocal call_count
                call_count += 1
                raise RuntimeError("boom")

        conn = FakeConn(CountingCursor())
        db = PgDatabase("postgres://example", use_pool=False)
        monkeypatch.setattr(db, "_get_conn", lambda: conn)
        monkeypatch.setattr(db, "_put_conn", lambda c: None)

        # 1回目: 接続試行してキャッシュ
        assert db.test_connection() is False
        assert call_count == 1

        # 2回目: TTL 内はキャッシュから返す（接続試行なし）
        assert db.test_connection() is False
        assert call_count == 1

    def test_failure_cache_expires_after_ttl(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTL 経過後は再接続を試みる。"""
        import time

        call_count = 0

        class CountingCursor(FakeCursor):
            def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
                nonlocal call_count
                call_count += 1
                raise RuntimeError("boom")

        conn = FakeConn(CountingCursor())
        db = PgDatabase("postgres://example", use_pool=False)
        monkeypatch.setattr(db, "_get_conn", lambda: conn)
        monkeypatch.setattr(db, "_put_conn", lambda c: None)

        # 1回目: 失敗してキャッシュ
        assert db.test_connection() is False
        assert call_count == 1

        # TTL を過去に設定してキャッシュを期限切れにする
        result, _ = db._probe_cache  # type: ignore[misc]
        db._probe_cache = (result, time.monotonic() - db._PROBE_TTL - 1.0)

        # 2回目: TTL 経過後は再試行する
        assert db.test_connection() is False
        assert call_count == 2

    def test_success_clears_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """成功時はキャッシュをクリアして以降も毎回テストを行う。"""
        call_count = 0

        class CountingCursor(FakeCursor):
            def __init__(self) -> None:
                super().__init__(fetchone_result=(1,))

            def execute(self, sql: str, params=None) -> None:  # noqa: ANN001
                nonlocal call_count
                call_count += 1

        conn = FakeConn(CountingCursor())
        db = PgDatabase("postgres://example", use_pool=False)
        monkeypatch.setattr(db, "_get_conn", lambda: conn)
        monkeypatch.setattr(db, "_put_conn", lambda c: None)

        # 失敗キャッシュを事前に設定
        import time
        db._probe_cache = (False, time.monotonic())

        # 成功するよう cursor を差し替え（キャッシュは失敗なので試行は行われない）
        # ただし TTL 内なのでキャッシュを使う → False を返す
        assert db.test_connection() is False
        assert call_count == 0

    def test_success_when_no_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """初回成功時はキャッシュなしで True を返しキャッシュも None のまま。"""
        cursor = FakeCursor(fetchone_result=(1,))
        conn = FakeConn(cursor)
        db = PgDatabase("postgres://example", use_pool=False)
        monkeypatch.setattr(db, "_get_conn", lambda: conn)
        monkeypatch.setattr(db, "_put_conn", lambda c: None)

        assert db.test_connection() is True
        # 成功時はキャッシュしない
        assert db._probe_cache is None
