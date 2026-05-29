"""context のテスト"""

import time
from pathlib import Path

import pytest

from devgear.mem.context import _format_timestamp, _select_within_budget, _truncate, build_context, importance_score
from devgear.mem.database import Database, MemoryChunk
from devgear.mem.settings import Settings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで Settings の保存先を一時ディレクトリに固定する。"""
    import devgear.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(context_chunk_count=50)


class TestBuildContext:
    """コンテキスト生成のテスト"""

    def test_empty_db(self, db: Database, settings: Settings) -> None:
        ctx = build_context(db, settings)
        assert ctx == ""

    def test_with_chunks(self, db: Database, settings: Settings) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="did some work",
                tool_names=["Edit"],
                files_read=[],
                files_modified=["file.py"],
                user_prompt="fix the bug",
                created_at_epoch=1700000000,
            )
        )
        ctx = build_context(db, settings)
        assert "<mem-context>" in ctx
        assert "</mem-context>" in ctx
        assert "fix the bug" in ctx
        assert "Edit" in ctx
        assert "file.py" in ctx
        assert "did some work" in ctx

    def test_project_filter(self, db: Database, settings: Settings) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj-a",
                chunk_index=0,
                content="work a",
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
                content="work b",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000001,
            )
        )
        ctx = build_context(db, settings, project="proj-a")
        assert "work a" in ctx
        assert "work b" not in ctx

    def test_session_grouping(self, db: Database, settings: Settings) -> None:
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
        ctx = build_context(db, settings)
        # セッションヘッダーは1回だけ
        assert ctx.count("## セッション:") == 1

    def test_multiple_sessions(self, db: Database, settings: Settings) -> None:
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="work 1",
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
                project="proj",
                chunk_index=0,
                content="work 2",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700001000,
            )
        )
        ctx = build_context(db, settings)
        assert ctx.count("## セッション:") == 2

    def test_no_prompt_no_tools(self, db: Database, settings: Settings) -> None:
        """プロンプトもツールもない場合でもクラッシュしない"""
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="bare content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=1700000000,
            )
        )
        ctx = build_context(db, settings)
        assert "bare content" in ctx
        assert "**Prompt**" not in ctx
        assert "**Tools**" not in ctx
        assert "**Modified**" not in ctx


class TestImportanceScore:
    """重要度スコアのテスト"""

    def _chunk(self, content="", tool_names=None, files_modified=None, access_count=0):
        return MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=0,
            content=content,
            tool_names=tool_names or [],
            files_read=[],
            files_modified=files_modified or [],
            user_prompt="",
            created_at_epoch=int(time.time()),
            access_count=access_count,
        )

    def test_score_range(self) -> None:
        chunk = self._chunk(
            content="x" * 500, tool_names=["Edit", "Read", "Write"], files_modified=["a.py"], access_count=5
        )
        score = importance_score(chunk)
        assert 0.0 <= score <= 1.0

    def test_file_modified_raises_score(self) -> None:
        with_mod = self._chunk(content="x" * 100, files_modified=["a.py"])
        without_mod = self._chunk(content="x" * 100, files_modified=[])
        assert importance_score(with_mod) > importance_score(without_mod)

    def test_access_count_raises_score(self) -> None:
        popular = self._chunk(content="x" * 100, access_count=5)
        unpopular = self._chunk(content="x" * 100, access_count=0)
        assert importance_score(popular) > importance_score(unpopular)

    def test_empty_chunk_low_score(self) -> None:
        chunk = self._chunk(content="")
        score = importance_score(chunk)
        assert score < 0.5


class TestBuildContextTokenBudget:
    """トークン予算制のテスト"""

    def test_token_budget_limits_output(self, db: Database) -> None:
        # 非常に小さいトークン予算を設定
        small_settings = Settings(context_max_tokens=1, context_chunk_count=50)
        for i in range(5):
            db.store_chunk(
                MemoryChunk(
                    session_id="s1",
                    project="proj",
                    chunk_index=i,
                    content="x" * 200,
                    tool_names=["Edit"],
                    files_read=[],
                    files_modified=["f.py"],
                    user_prompt="do stuff",
                    created_at_epoch=1700000000 + i,
                )
            )
        ctx = build_context(db, small_settings)
        # 予算が小さすぎるため何も注入されない（または空）
        assert ctx == "" or len(ctx) < 500


class TestFormatTimestamp:
    def test_format(self) -> None:
        result = _format_timestamp(1700000000)
        assert "2023-11-14" in result

    def test_utc(self) -> None:
        result = _format_timestamp(0)
        assert "1970-01-01 00:00" == result


class TestTruncate:
    def test_short_text(self) -> None:
        assert _truncate("hello", 100) == "hello"

    def test_long_text(self) -> None:
        result = _truncate("a" * 300, 200)
        assert result.endswith("...")
        assert len(result) == 203  # 200 + "..."

    def test_exact_length(self) -> None:
        assert _truncate("abc", 3) == "abc"


class TestSelectWithinBudget:
    def test_stops_when_next_chunk_does_not_fit(self) -> None:
        chunk_a = MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=0,
            content="fit",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=1700000000,
        )
        chunk_b = MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=1,
            content="x" * 100,
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=1700000001,
        )

        selected = _select_within_budget([(chunk_a, 1.0), (chunk_b, 0.5)], max_tokens=4)

        assert selected == [chunk_a]
