"""team: exclude_origin_user kwarg と fetch_chunks_by_ids のテスト。"""

from __future__ import annotations

import pytest

from devgear.mem.pg_database import PgDatabase
from tests.mem.test_pg_database import FakeConn, FakeCursor


def _db_with(monkeypatch: pytest.MonkeyPatch, cursor: FakeCursor) -> tuple[PgDatabase, FakeConn]:
    conn = FakeConn(cursor)
    db = PgDatabase("postgres://example", use_pool=False)
    monkeypatch.setattr(db, "_get_conn", lambda: conn)
    monkeypatch.setattr(db, "_put_conn", lambda _c: None)
    return db, conn


def test_fts_search_exclude_origin_user_injects_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude_origin_user 指定時の SQL に ``origin_user <> %s`` が含まれること。"""
    cursor = FakeCursor(fetchall_result=[("c-1", 0.9)])
    db, _conn = _db_with(monkeypatch, cursor)

    result = db.fts_search("hello", limit=3, exclude_origin_user="alice")

    assert result == [("c-1", 0.9)]
    sql, params = cursor.executed[-1]
    assert "origin_user <> %s" in sql
    assert params is not None
    assert "alice" in params


def test_fts_search_without_exclude_uses_plain_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(fetchall_result=[])
    db, _conn = _db_with(monkeypatch, cursor)

    db.fts_search("hello", limit=3)

    sql, _ = cursor.executed[-1]
    assert "origin_user" not in sql


def test_vec_search_exclude_joins_memory_chunks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """exclude_origin_user 指定時は memory_chunks との JOIN になること。"""
    cursor = FakeCursor(fetchall_result=[("c-2", 0.1)])
    db, _conn = _db_with(monkeypatch, cursor)

    db.vec_search([0.1, 0.2], limit=2, exclude_origin_user="bob")

    sql, params = cursor.executed[-1]
    assert "JOIN memory_chunks" in sql
    assert "c.origin_user <> %s" in sql
    assert params is not None
    assert "bob" in params


def test_vec_search_without_exclude_is_plain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cursor = FakeCursor(fetchall_result=[])
    db, _conn = _db_with(monkeypatch, cursor)

    db.vec_search([0.1, 0.2], limit=2)

    sql, _ = cursor.executed[-1]
    assert "JOIN" not in sql


def test_team_search_propagates_exclude_to_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """team_search が fts_search / vec_search の両方に exclude を伝搬すること。"""
    db = PgDatabase("postgres://example", use_pool=False)

    fts_calls: list[dict] = []
    vec_calls: list[dict] = []

    def _fake_fts(query: str, limit: int = 20, *, exclude_origin_user=None):  # noqa: ANN001
        fts_calls.append({"q": query, "limit": limit, "excl": exclude_origin_user})
        return [("a", 0.9)]

    def _fake_vec(embedding, limit: int = 20, *, exclude_origin_user=None):  # noqa: ANN001
        vec_calls.append({"emb": embedding, "limit": limit, "excl": exclude_origin_user})
        return [("a", 0.1), ("b", 0.2)]

    monkeypatch.setattr(db, "fts_search", _fake_fts)
    monkeypatch.setattr(db, "vec_search", _fake_vec)

    ranked = db.team_search("hello", [0.1], limit=5, exclude_origin_user="carol")

    assert [cid for cid, _ in ranked][0] == "a"
    assert fts_calls[0]["excl"] == "carol"
    assert vec_calls[0]["excl"] == "carol"


def test_fetch_chunks_by_ids_returns_empty_on_empty_input() -> None:
    db = PgDatabase("postgres://example", use_pool=False)
    assert db.fetch_chunks_by_ids([]) == {}


def test_fetch_chunks_by_ids_maps_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    """DB 行がフィールド辞書に正しく展開されること。"""
    rows = [
        (
            "c-1",
            "alice",
            "content-1",
            "prompt-1",
            "proj",
            1_700_000_000,
            ["Edit"],
            ["a.py"],
            ["a.py"],
        ),
        (
            "c-2",
            "bob",
            "content-2",
            "prompt-2",
            "proj",
            1_700_001_000,
            '["Read"]',  # JSON 文字列パスも通る
            '["b.py"]',
            None,
        ),
    ]
    cursor = FakeCursor(fetchall_result=rows)
    db, _conn = _db_with(monkeypatch, cursor)

    result = db.fetch_chunks_by_ids(["c-1", "c-2"])

    assert set(result.keys()) == {"c-1", "c-2"}
    assert result["c-1"]["origin_user"] == "alice"
    assert result["c-1"]["tool_names"] == ["Edit"]
    assert result["c-2"]["tool_names"] == ["Read"]
    assert result["c-2"]["files_modified"] == []
    sql, params = cursor.executed[-1]
    assert "memory_chunks" in sql
    assert params == ["c-1", "c-2"]
