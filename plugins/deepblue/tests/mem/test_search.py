"""search のテスト"""

import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

from devgear.mem.database import Database, MemoryChunk
from devgear.mem.search import SearchService, _reciprocal_rank_fusion, adaptive_decay, should_inject_memory
from devgear.mem.settings import Settings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで Settings の保存先を一時ディレクトリに固定する。"""
    import devgear.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


@pytest.fixture(autouse=True)
def _patch_embed_query(monkeypatch: pytest.MonkeyPatch) -> None:
    """embed_query をモックして HF Hub への通信を防ぐ（local_files_only=True のため必要）。"""
    monkeypatch.setattr("devgear.mem.embedding.embed_query", lambda query, model: [0.1, 0.2])


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
                user_prompt="add JWT auth",
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
                user_prompt="fix db pool",
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
                user_prompt="",
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
                user_prompt="",
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
                user_prompt="",
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

    def test_search_team_branches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings()
        settings.sync.enabled = True
        settings.sync.postgres_url = "postgres://example"
        settings.embedding_model = "model"
        svc = SearchService(object(), settings)  # type: ignore[arg-type]

        monkeypatch.setattr(
            "devgear.mem.embedding.embed_query",
            lambda query, model: [0.1, 0.2],
        )

        class FakePg:
            connected = True
            team_results: list[tuple[str, float]] = []
            rows: dict[str, dict] = {}
            last: "FakePg | None" = None

            def __init__(self, url: str) -> None:
                self.url = url
                self.closed = False
                self.team_calls: list[dict[str, object]] = []
                self.fetch_calls: list[list[str]] = []
                FakePg.last = self

            def test_connection(self) -> bool:
                return FakePg.connected

            def team_search(
                self,
                query: str,
                embedding,
                limit: int = 20,
                *,
                exclude_origin_user=None,
            ):  # noqa: ANN001
                self.team_calls.append(
                    {
                        "query": query,
                        "embedding": embedding,
                        "limit": limit,
                        "exclude": exclude_origin_user,
                    }
                )
                return list(FakePg.team_results)

            def fetch_chunks_by_ids(self, chunk_ids: list[str]) -> dict[str, dict]:
                self.fetch_calls.append(list(chunk_ids))
                return dict(FakePg.rows)

            def close(self) -> None:
                self.closed = True

        fake_pg_mod = ModuleType("devgear.mem.pg_database")
        fake_pg_mod.PgDatabase = FakePg
        monkeypatch.setitem(sys.modules, "devgear.mem.pg_database", fake_pg_mod)

        settings.sync.enabled = False
        assert svc.search_team("query") == []

        settings.sync.enabled = True
        FakePg.connected = False
        assert svc.search_team("query") == []
        assert FakePg.last is not None and FakePg.last.closed is True

        FakePg.connected = True
        FakePg.team_results = []
        assert svc.search_team("query") == []
        assert FakePg.last is not None and FakePg.last.closed is True

        FakePg.team_results = [("c-1", 0.9), ("c-2", 0.8)]
        FakePg.rows = {
            "c-2": {
                "content": "content-2",
                "user_prompt": "prompt-2",
                "project": "proj",
                "created_at_epoch": 1,
                "tool_names": ["Edit"],
                "files_read": [],
                "files_modified": ["b.py"],
            }
        }
        results = svc.search_team("query", limit=1, exclude_origin_user="me")
        assert len(results) == 1
        assert results[0].chunk_id == "c-2"
        assert FakePg.last is not None
        assert FakePg.last.team_calls[-1]["exclude"] == "me"
        assert FakePg.last.closed is True


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
