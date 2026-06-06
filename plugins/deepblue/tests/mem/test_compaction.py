"""compaction のテスト"""

import time
from pathlib import Path

import pytest

from deepblue.mem.compaction import (
    detect_low_quality,
    memory_health_report,
    merge_chunks,
    optimize_db,
    prune_candidates,
)
from deepblue.mem.database import Database, MemoryChunk


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


def _make_chunk(
    session_id: str = "s1",
    project: str = "proj",
    chunk_index: int = 0,
    content: str = "some content that is long enough",
    tool_names: list | None = None,
    files_modified: list | None = None,
    created_at_epoch: int | None = None,
    access_count: int = 0,
) -> MemoryChunk:
    return MemoryChunk(
        session_id=session_id,
        project=project,
        chunk_index=chunk_index,
        content=content,
        tool_names=tool_names or [],
        files_read=[],
        files_modified=files_modified or [],
        user_prompt="",
        created_at_epoch=created_at_epoch or int(time.time()),
        access_count=access_count,
    )


class TestDetectLowQuality:
    def test_short_chunk_detected(self, db: Database) -> None:
        cid = db.store_chunk(_make_chunk(content="short"))
        candidates = detect_low_quality(db)
        assert cid in candidates

    def test_normal_chunk_not_detected(self, db: Database) -> None:
        cid = db.store_chunk(_make_chunk(content="x" * 100))
        candidates = detect_low_quality(db)
        assert cid not in candidates

    def test_old_readonly_chunk_detected(self, db: Database) -> None:
        old_epoch = int(time.time()) - 91 * 86400
        cid = db.store_chunk(
            _make_chunk(
                content="x" * 100,
                tool_names=["Read", "Grep"],
                files_modified=[],
                created_at_epoch=old_epoch,
            )
        )
        candidates = detect_low_quality(db)
        assert cid in candidates

    def test_old_chunk_with_edit_not_detected(self, db: Database) -> None:
        old_epoch = int(time.time()) - 91 * 86400
        cid = db.store_chunk(
            _make_chunk(
                content="x" * 100,
                tool_names=["Edit"],
                files_modified=["file.py"],
                created_at_epoch=old_epoch,
            )
        )
        candidates = detect_low_quality(db)
        assert cid not in candidates

    def test_recent_readonly_not_detected(self, db: Database) -> None:
        cid = db.store_chunk(
            _make_chunk(
                content="x" * 100,
                tool_names=["Read"],
                files_modified=[],
                created_at_epoch=int(time.time()),
            )
        )
        candidates = detect_low_quality(db)
        assert cid not in candidates

    def test_no_duplicates_in_result(self, db: Database) -> None:
        # 短くかつ古いRead-onlyチャンク → 重複なし
        old_epoch = int(time.time()) - 91 * 86400
        db.store_chunk(_make_chunk(content="sh", tool_names=["Read"], created_at_epoch=old_epoch))
        candidates = detect_low_quality(db)
        assert len(candidates) == len(set(candidates))


class TestMergeChunks:
    def test_uses_newest_as_base(self) -> None:
        old = _make_chunk(content="old content", created_at_epoch=1000000, tool_names=["Read"], files_modified=[])
        new = _make_chunk(content="new content", created_at_epoch=2000000, tool_names=["Edit"], files_modified=["a.py"])
        merged = merge_chunks([old, new])
        assert merged.content == "new content"
        assert merged.created_at_epoch == 2000000

    def test_tool_names_union(self) -> None:
        c1 = _make_chunk(tool_names=["Read", "Edit"])
        c2 = _make_chunk(tool_names=["Edit", "Write"])
        merged = merge_chunks([c1, c2])
        assert set(merged.tool_names) == {"Read", "Edit", "Write"}

    def test_files_modified_union(self) -> None:
        c1 = _make_chunk(files_modified=["a.py"])
        c2 = _make_chunk(files_modified=["b.py"])
        merged = merge_chunks([c1, c2])
        assert set(merged.files_modified) == {"a.py", "b.py"}

    def test_generation_incremented(self) -> None:
        c1 = _make_chunk()
        c1.merged_generation = 0
        c2 = _make_chunk()
        c2.merged_generation = 1
        merged = merge_chunks([c1, c2])
        assert merged.merged_generation == 2

    def test_single_chunk(self) -> None:
        chunk = _make_chunk(content="solo", created_at_epoch=1000000)
        merged = merge_chunks([chunk])
        assert merged.content == "solo"
        assert merged.merged_generation == 1


class TestPruneCandidates:
    def test_very_old_unaccessed_chunk_pruned(self, db: Database) -> None:
        # 非常に古いチャンク（減衰 < 0.01）でアクセスなし
        very_old = int(time.time()) - 500 * 86400  # 500日前
        cid = db.store_chunk(_make_chunk(created_at_epoch=very_old, access_count=0))
        candidates = prune_candidates(db)
        assert cid in candidates

    def test_accessed_chunk_not_pruned(self, db: Database) -> None:
        very_old = int(time.time()) - 500 * 86400
        cid = db.store_chunk(_make_chunk(created_at_epoch=very_old))
        db.update_access([cid])  # access_count = 1 をDBに反映
        candidates = prune_candidates(db)
        assert cid not in candidates

    def test_recent_chunk_not_pruned(self, db: Database) -> None:
        cid = db.store_chunk(_make_chunk(created_at_epoch=int(time.time())))
        candidates = prune_candidates(db)
        assert cid not in candidates


class TestOptimizeDb:
    def test_returns_dict(self, db: Database) -> None:
        result = optimize_db(db)
        assert isinstance(result, dict)
        assert "fragmentation_before" in result

    def test_fragmentation_in_range(self, db: Database) -> None:
        result = optimize_db(db)
        assert 0.0 <= result["fragmentation_before"] <= 1.0

    def test_runs_without_data(self, db: Database) -> None:
        # 空DBでも失敗しない
        result = optimize_db(db)
        assert result is not None


class TestMemoryHealthReport:
    def test_empty_db(self, db: Database) -> None:
        report = memory_health_report(db)
        assert report["total_chunks"] == 0
        assert report["short_chunk_pct"] == 0
        assert report["db_size_mb"] >= 0

    def test_with_chunks(self, db: Database) -> None:
        db.store_chunk(_make_chunk(content="x" * 100))
        db.store_chunk(_make_chunk(content="short", chunk_index=1))
        report = memory_health_report(db)
        assert report["total_chunks"] == 2
        assert report["short_chunk_pct"] == 50.0
        assert report["avg_chunk_size"] > 0

    def test_report_keys(self, db: Database) -> None:
        report = memory_health_report(db)
        assert set(report.keys()) == {"total_chunks", "db_size_mb", "short_chunk_pct", "avg_chunk_size"}


class TestOptimizeDbVacuum:
    def test_vacuum_runs_on_high_fragmentation(self, db: Database) -> None:
        """断片化率が高い場合に VACUUM が実行される"""
        from unittest.mock import MagicMock

        original_conn = db.conn
        original_execute = original_conn.execute

        def mock_execute(sql, *args, **kwargs):
            if "PRAGMA freelist_count" in sql:
                m = MagicMock()
                m.fetchone.return_value = [20]  # 20 フリーページ
                return m
            if "PRAGMA page_count" in sql:
                m = MagicMock()
                m.fetchone.return_value = [100]  # 合計100ページ → 断片化 20%
                return m
            if "VACUUM" in sql or "wal_checkpoint" in sql:
                # VACUUM はトランザクション外でないと失敗するためテストでは空 Mock で代替
                return MagicMock()
            return original_execute(sql, *args, **kwargs)

        mock_conn = MagicMock(wraps=original_conn)
        mock_conn.execute.side_effect = mock_execute
        db.conn = mock_conn
        try:
            result = optimize_db(db)
        finally:
            db.conn = original_conn
        assert result["fragmentation_before"] == pytest.approx(0.20)

    def test_vacuum_succeeds_without_transaction_error(self, db: Database) -> None:
        """断片化率が高い実 DB で VACUUM が "cannot VACUUM from within a transaction" を起こさない。"""
        # 大量挿入 → 大半削除で断片化 15% 超を作る
        for i in range(150):
            db.store_chunk(_make_chunk(chunk_index=i, content="x" * 300))
        db.conn.execute("DELETE FROM memory_chunks WHERE chunk_index < 130")
        db.conn.commit()

        free = db.conn.execute("PRAGMA freelist_count").fetchone()[0]
        pages = db.conn.execute("PRAGMA page_count").fetchone()[0]
        assert free / pages > 0.15, "前提条件: 断片化率が 15% を超えていること"

        # OperationalError が発生しなければ OK
        result = optimize_db(db)
        assert result["fragmentation_before"] > 0.15

    def test_fts5_optimize_exception_is_ignored(self, db: Database) -> None:
        """FTS5 optimize が失敗しても optimize_db はクラッシュしない"""
        from unittest.mock import MagicMock

        original_conn = db.conn
        original_execute = original_conn.execute

        def mock_execute(sql, *args, **kwargs):
            if "memory_chunks_fts" in sql:
                raise Exception("fts5 not available")
            return original_execute(sql, *args, **kwargs)

        mock_conn = MagicMock(wraps=original_conn)
        mock_conn.execute.side_effect = mock_execute
        db.conn = mock_conn
        try:
            result = optimize_db(db)
        finally:
            db.conn = original_conn
        assert "fragmentation_before" in result


def test_optimize_db_vacuum_failure(db: Database) -> None:
    """断片化率が閾値超でも VACUUM が失敗すれば vacuumed=False を返す。"""
    import sqlite3
    from types import SimpleNamespace

    class _FakeConn:
        def execute(self, sql, *args):
            if "freelist_count" in sql:
                return SimpleNamespace(fetchone=lambda: (20,))
            if "page_count" in sql:
                return SimpleNamespace(fetchone=lambda: (100,))  # frag=0.2 > 0.15
            if sql == "VACUUM":
                raise sqlite3.OperationalError("cannot VACUUM")
            return SimpleNamespace(fetchone=lambda: (0,))

        def commit(self) -> None:
            pass

    original = db.conn
    db.conn = _FakeConn()
    try:
        result = optimize_db(db)
    finally:
        db.conn = original
    assert result["vacuumed"] is False
    assert result["fragmentation_before"] == 0.2
