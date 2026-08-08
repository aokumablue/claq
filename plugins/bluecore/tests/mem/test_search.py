"""search のテスト"""

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from bluecore.mem.database import Database, MemoryChunk
from bluecore.mem.models import SessionDigest
from bluecore.mem.search import (
    DigestSearchResult,
    SearchService,
    _reciprocal_rank_fusion,
    adaptive_decay,
    should_inject_memory,
)
from bluecore.mem.settings import Settings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで Settings の保存先を一時ディレクトリに固定する。"""
    import bluecore.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _patch_embed_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_query をモックして HF Hub への通信を防ぐ（local_files_only=True のため必要）。"""
    monkeypatch.setattr("bluecore.mem.embedding.embed_query", lambda query, model: [0.1, 0.2])


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings()


class TestRRF:
    """Reciprocal Rank Fusion のテスト"""

    def test_single_list(self) -> None:
        result = _reciprocal_rank_fusion([10, 20, 30], [])
        assert len(result) == 3
        # 1位のスコアが最も高い
        assert result[0][0] == 10

    def test_merge_lists(self) -> None:
        result = _reciprocal_rank_fusion([10, 20], [20, 30])
        ids = [cid for cid, _ in result]
        # ID 20 は両方に出現するため最高スコア
        assert ids[0] == 20

    def test_empty(self) -> None:
        assert _reciprocal_rank_fusion([], []) == []


class TestSearchService:
    """検索サービスの統合テスト"""

    def test_fts_search_integration(self, db: Database, settings: Settings) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="myproj",
                chunk_index=0,
                content="implemented user authentication with JWT tokens",
                tool_names=["Edit"],
                files_read=[],
                files_modified=["auth.py"],
                created_at_epoch=int(time.time()),
            )
        )
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="myproj",
                chunk_index=1,
                content="refactored database connection pooling",
                tool_names=["Edit"],
                files_read=[],
                files_modified=["db.py"],
                created_at_epoch=int(time.time()),
            )
        )

        svc = SearchService(db, settings)
        results = svc.search("authentication")
        assert len(results) >= 1
        assert "authentication" in results[0].content

    def test_project_filter(self, db: Database, settings: Settings) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj-a",
                chunk_index=0,
                content="work on project A",
                tool_names=[],
                files_read=[],
                files_modified=[],
                created_at_epoch=int(time.time()),
            )
        )
        db.store_chunk(
            MemoryChunk(
                session_id="s2",
                project="proj-b",
                chunk_index=0,
                content="work on project B",
                tool_names=[],
                files_read=[],
                files_modified=[],
                created_at_epoch=int(time.time()),
            )
        )

        svc = SearchService(db, settings)

        # プロジェクトフィルタなし → 両方ヒット
        all_results = svc.search("work on project")
        assert len(all_results) == 2

        # プロジェクトフィルタあり → 1件のみ
        filtered = svc.search("work on project", project="proj-a")
        assert len(filtered) == 1
        assert filtered[0].project == "proj-a"

    def test_chunk_not_found_in_batch(self, db: Database, settings: Settings) -> None:
        """RRF で返された chunk_id が DB に存在しない場合、スキップされる"""
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="existing chunk",
                tool_names=[],
                files_read=[],
                files_modified=[],
                created_at_epoch=int(time.time()),
            )
        )
        svc = SearchService(db, settings)
        # fts_search が存在しない ID を返すようにモック
        with patch.object(db, "fts_search", return_value=[(1, 0.5), (99999, 0.3)]):
            results = svc.search("existing chunk")
        # 存在する ID のみ結果に含まれる
        chunk_ids = [r.chunk_id for r in results]
        assert 99999 not in chunk_ids

def _make_digest(
    *,
    session_id: str,
    project: str = "proj",
    summary: str = "summary",
    started_at_epoch: int | None = None,
    created_at_epoch: int | None = None,
) -> SessionDigest:
    """テスト用の SessionDigest を構築する。"""
    now = int(time.time())
    return SessionDigest(
        session_id=session_id,
        project=project,
        summary=summary,
        started_at_epoch=started_at_epoch if started_at_epoch is not None else now,
        created_at_epoch=created_at_epoch if created_at_epoch is not None else now,
    )


class TestSearchDigests:
    """SearchService.search_digests のテスト"""

    def test_hit(self, db: Database, settings: Settings) -> None:
        db.upsert_session_digest(
            _make_digest(session_id="s1", summary="fixed authentication regression in login flow")
        )
        svc = SearchService(db, settings)
        results = svc.search_digests("authentication")
        assert len(results) == 1
        assert isinstance(results[0], DigestSearchResult)
        assert results[0].digest.session_id == "s1"
        assert results[0].score > 0

    def test_no_results(self, db: Database, settings: Settings) -> None:
        svc = SearchService(db, settings)
        assert svc.search_digests("xyznonexistent") == []

    def test_project_filter(self, db: Database, settings: Settings) -> None:
        db.upsert_session_digest(
            _make_digest(session_id="a", project="proj-a", summary="work on alpha module")
        )
        db.upsert_session_digest(
            _make_digest(session_id="b", project="proj-b", summary="work on alpha module too")
        )
        svc = SearchService(db, settings)

        all_results = svc.search_digests("alpha module", limit=10)
        assert len(all_results) == 2

        filtered = svc.search_digests("alpha module", project="proj-a", limit=10)
        assert len(filtered) == 1
        assert filtered[0].digest.project == "proj-a"

    def test_limit(self, db: Database, settings: Settings) -> None:
        for i in range(3):
            db.upsert_session_digest(
                _make_digest(session_id=f"s{i}", summary=f"database migration attempt {i}")
            )
        svc = SearchService(db, settings)
        results = svc.search_digests("database migration", limit=2)
        assert len(results) == 2

    def test_decay_applied_recent_ranks_above_old(
        self, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同じ FTS 順位でも、時間減衰により新しい digest の方が高スコアになる。"""
        old_epoch = int(time.time()) - 365 * 86400
        recent_epoch = int(time.time())
        old_digest = _make_digest(
            session_id="old", summary="old summary", created_at_epoch=old_epoch, started_at_epoch=old_epoch
        )
        recent_digest = _make_digest(
            session_id="recent",
            summary="recent summary",
            created_at_epoch=recent_epoch,
            started_at_epoch=recent_epoch,
        )
        old_id = db.upsert_session_digest(old_digest)
        recent_id = db.upsert_session_digest(recent_digest)

        # FTS の生の順位は同点（old が先）と仮定し、decay の影響のみを検証する
        monkeypatch.setattr(db, "fts_search_digests", lambda query, limit=10: [(old_id, -1.0), (recent_id, -1.0)])

        svc = SearchService(db, settings)
        results = svc.search_digests("anything", limit=10)
        assert results[0].digest.session_id == "recent"
        assert results[0].score > results[1].score

    def test_digest_id_not_found_is_skipped(self, db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
        """FTS が返した digest_id が DB に存在しない場合はスキップされる。"""
        monkeypatch.setattr(db, "fts_search_digests", lambda query, limit=10: [("missing-id", -1.0)])
        svc = SearchService(db, settings)
        assert svc.search_digests("anything") == []


class TestAdaptiveDecay:
    """adaptive_decay のテスト"""

    def test_recent_no_access(self) -> None:
        # 最近のチャンク・アクセスなし → ほぼ 1.0
        assert adaptive_decay(int(time.time()), None, 0) == pytest.approx(1.0, abs=0.01)

    def test_half_life_no_access(self) -> None:
        # 30日前・アクセスなし → 0.5
        epoch_30d_ago = int(time.time()) - 30 * 86400
        assert adaptive_decay(epoch_30d_ago, None, 0, base_half_life=30.0) == pytest.approx(0.5, abs=0.01)

    def test_access_extends_half_life(self) -> None:
        # アクセスが多いほど減衰が遅くなる
        epoch_30d_ago = int(time.time()) - 30 * 86400
        decay_no_access = adaptive_decay(epoch_30d_ago, None, 0, base_half_life=30.0)
        decay_with_access = adaptive_decay(epoch_30d_ago, None, 5, base_half_life=30.0)
        assert decay_with_access > decay_no_access

    def test_half_life_capped_at_180_days(self) -> None:
        # access_count が大きくても半減期は 180 日が上限
        # 180日前のチャンク・アクセス回数が十分多い場合、減衰は 0.5 付近
        epoch_180d_ago = int(time.time()) - 180 * 86400
        # cap=180日、age=180日 → decay ≈ 0.5 に近いはず
        result = adaptive_decay(epoch_180d_ago, None, 100, base_half_life=30.0)
        assert result == pytest.approx(0.5, abs=0.05)

    def test_last_accessed_epoch_used(self) -> None:
        # created_at が古くても最近アクセスされた場合、減衰しない
        old_epoch = int(time.time()) - 365 * 86400
        recent_epoch = int(time.time())
        result = adaptive_decay(old_epoch, recent_epoch, 1, base_half_life=30.0)
        assert result == pytest.approx(1.0, abs=0.01)


class TestShouldInjectMemory:
    """should_inject_memory のテスト"""

    @pytest.mark.parametrize(
        "prompt",
        [
            "前回のDBマイグレーションどうやったっけ？",
            "以前実装したやつを確認したい",
            "last time we did this",
            "how did we solve this before",
        ],
    )
    def test_retrospective_patterns(self, prompt: str) -> None:
        assert should_inject_memory(prompt) is True

    @pytest.mark.parametrize(
        "prompt",
        [
            "また同じエラーが出た",
            "similar approach again",
            "新しい機能を追加したい",
            "このファイルを読んで",
            "テストを書いてください",
            "implement a new API endpoint",
        ],
    )
    def test_new_task_patterns(self, prompt: str) -> None:
        assert should_inject_memory(prompt) is False

    def test_empty_prompt(self) -> None:
        assert should_inject_memory("") is False
