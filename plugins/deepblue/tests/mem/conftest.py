"""mem テスト共通フィクスチャ・スキップ設定"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from devgear.mem.database import MemoryChunk


def pytest_collection_modifyitems(items: list) -> None:
    """sqlite-vec が未インストールの場合はテストをスキップ"""
    try:
        import sqlite_vec  # noqa: F401

        has_sqlite_vec = True
    except ImportError:
        has_sqlite_vec = False

    skip_vec = pytest.mark.skip(reason="sqlite-vec not installed")

    for item in items:
        if "tests/mem" not in str(item.fspath):
            continue
        # embedding / _paths / settings は sqlite-vec に依存しないのでスキップ除外
        if item.fspath.basename in {
            "test_embedding_security.py", "test_embedding.py",
            "test_paths.py", "test_settings.py", "test_settings_slim.py",
            "test_cli.py",
        }:
            continue
        if not has_sqlite_vec:
            item.add_marker(skip_vec)


def make_settings(tmp_path: Path, *, auto_compact_enabled: bool = True) -> SimpleNamespace:
    """最低限の Settings 互換オブジェクトを作成する。"""
    return SimpleNamespace(
        db_path=tmp_path / "mem.db",
        data_path=tmp_path,
        log_dir=tmp_path / "logs",
        log_level="INFO",
        sync=SimpleNamespace(
            enabled=True,
            postgres_url="postgres://user:pass@localhost/db",
            origin_user="user",
        ),
        chunk_max_length=200,
        embedding_model="model",
        auto_compact_enabled=auto_compact_enabled,
        auto_compact_interval_days=0,
        last_compacted_at=0,
        excluded_projects=set(),
        save=lambda: None,
        save_sync_state=lambda: None,
    )


class FakeDB:
    """テスト用インメモリ DB スタブ。"""

    def __init__(self, chunks: list[MemoryChunk] | None = None) -> None:
        self.chunks: list[MemoryChunk] = chunks or []
        self.chunk_map: dict = {c.id: c for c in self.chunks if c.id is not None}
        self.executed: list[tuple[str, object]] = []
        self.embeddings: list[tuple[list, list]] = []
        self.sessions: list[object] = []
        self.stored_chunks: list[MemoryChunk] = []
        self.interactions: list[object] = []
        self.project_profiles: dict[str, object] = {}
        self.item_runs: list[object] = []
        self.conn = SimpleNamespace(execute=self.execute, commit=self.commit)

    def __enter__(self) -> FakeDB:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params=None):  # noqa: ANN001
        self.executed.append((sql, params))
        return SimpleNamespace(fetchone=lambda: (0,), fetchall=lambda: [])

    def commit(self) -> None:
        self.executed.append(("commit", None))

    def close(self) -> None:
        pass

    def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:  # noqa: ANN001
        return self.chunks

    def store_embeddings(self, ids, embeddings) -> None:  # noqa: ANN001
        self.embeddings.append((list(ids), list(embeddings)))

    def get_recent_chunks(self, limit: int = 50, project: str | None = None) -> list[MemoryChunk]:  # noqa: ANN001
        return self.chunks

    def get_chunks_by_ids(self, ids) -> dict:  # noqa: ANN001
        return {cid: self.chunk_map[cid] for cid in ids if cid in self.chunk_map}

    def get_chunk_by_id(self, chunk_id) -> MemoryChunk | None:  # noqa: ANN001
        return self.chunk_map.get(chunk_id)

    def get_next_chunk_index(self, session_id: str) -> int:  # noqa: ANN001
        return len(self.chunks)

    def upsert_session(self, session) -> None:  # noqa: ANN001
        self.sessions.append(session)

    def store_chunk(self, chunk: MemoryChunk):  # noqa: ANN001
        if chunk.id is None:
            chunk.id = f"chunk-{len(self.stored_chunks) + 1}"
        self.stored_chunks.append(chunk)
        self.chunk_map[chunk.id] = chunk
        self.chunks.append(chunk)
        return chunk.id

    def get_next_interaction_index(self, session_id: str) -> int:  # noqa: ANN001
        return len(self.interactions)

    def store_interaction_log(self, interaction) -> str:  # noqa: ANN001
        self.interactions.append(interaction)
        return f"interaction-{len(self.interactions)}"

    def upsert_project_profile(self, profile) -> str:  # noqa: ANN001
        self.project_profiles[profile.project] = profile
        return f"profile-{len(self.project_profiles)}"

    def get_project_profile(self, project: str, origin_user: str | None = None):  # noqa: ANN001
        return self.project_profiles.get(project)

    def store_mem_item_run(self, run) -> str:  # noqa: ANN001
        self.item_runs.append(run)
        return f"run-{len(self.item_runs)}"


@contextmanager
def open_fake_db(db: FakeDB):
    """FakeDB をコンテキストマネージャとして使うためのヘルパー。"""
    yield db
