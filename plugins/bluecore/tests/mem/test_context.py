"""context のテスト（hot + digest 2層メモリ）"""

import time
from pathlib import Path

import pytest

from bluecore.mem.context import (
    _filter_hot_chunks,
    _format_date,
    _format_digest,
    _format_timestamp,
    _select_digests_within_budget,
    _select_within_budget,
    build_context,
    importance_score,
)
from bluecore.mem.database import Database, MemoryChunk
from bluecore.mem.models import SessionDigest
from bluecore.mem.settings import Settings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで Settings の保存先を一時ディレクトリに固定する。"""
    import bluecore.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(context_chunk_count=50)


def _now() -> int:
    """テスト実行時刻（hot 層の 24h ウィンドウ内に収まる epoch 秒）を返す。"""
    return int(time.time())


def _make_digest(
    *,
    session_id: str,
    project: str = "proj",
    summary: str = "summary",
    started_at_epoch: int,
    created_at_epoch: int,
    key_files: list[str] | None = None,
    key_decisions: list[str] | None = None,
    outcome: str = "success",
) -> SessionDigest:
    """テスト用の SessionDigest を構築する。"""
    return SessionDigest(
        session_id=session_id,
        project=project,
        summary=summary,
        started_at_epoch=started_at_epoch,
        created_at_epoch=created_at_epoch,
        key_files=key_files or [],
        key_decisions=key_decisions or [],
        outcome=outcome,
    )


class TestBuildContext:
    """コンテキスト生成のテスト（hot 層）"""

    def test_empty_db(self, db: Database, settings: Settings) -> None:
        ctx = build_context(db, settings)
        assert ctx == ""

    def test_with_chunks(self, db: Database, settings: Settings) -> None:
        now = _now()
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
                created_at_epoch=now,
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
        now = _now()
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
                created_at_epoch=now,
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
                created_at_epoch=now,
            )
        )
        ctx = build_context(db, settings, project="proj-a")
        assert "work a" in ctx
        assert "work b" not in ctx

    def test_session_grouping(self, db: Database, settings: Settings) -> None:
        now = _now()
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
                    created_at_epoch=now + i,
                )
            )
        ctx = build_context(db, settings)
        # セッションヘッダーは1回だけ
        assert ctx.count("## セッション:") == 1

    def test_multiple_sessions(self, db: Database, settings: Settings) -> None:
        now = _now()
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
                created_at_epoch=now,
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
                created_at_epoch=now + 10,
            )
        )
        ctx = build_context(db, settings)
        assert ctx.count("## セッション:") == 2

    def test_empty_content_chunk(self, db: Database, settings: Settings) -> None:
        """content が空でもプロンプト等は注入され、本文ブロックは省略される。"""
        now = _now()
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="",
                tool_names=["Edit"],
                files_read=[],
                files_modified=[],
                user_prompt="empty content prompt",
                created_at_epoch=now,
            )
        )
        ctx = build_context(db, settings)
        assert "empty content prompt" in ctx
        assert "```" not in ctx

    def test_no_prompt_no_tools(self, db: Database, settings: Settings) -> None:
        """プロンプトもツールもない場合でもクラッシュしない"""
        now = _now()
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
                created_at_epoch=now,
            )
        )
        ctx = build_context(db, settings)
        assert "bare content" in ctx
        assert "**Prompt**" not in ctx
        assert "**Tools**" not in ctx
        assert "**Modified**" not in ctx

    def test_old_chunk_outside_hot_window_is_excluded(self, db: Database, settings: Settings) -> None:
        """hot_hours を超える古いチャンクは hot 層に含まれない（digest も無ければ空になる）。"""
        old_epoch = _now() - (settings.context_hot_hours * 3600 + 3600)
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="ancient work",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=old_epoch,
            )
        )
        ctx = build_context(db, settings)
        assert ctx == ""


class TestBuildContextDigestLayer:
    """コンテキスト生成のテスト（digest 層と重複除外）"""

    def test_digest_included_when_no_hot_overlap(self, db: Database, settings: Settings) -> None:
        """hot 層に無いセッションの digest はそのまま注入される。"""
        digest = _make_digest(
            session_id="past-session",
            summary="past work summary",
            started_at_epoch=1700000000,
            created_at_epoch=1700000100,
            key_files=["a.py"],
            key_decisions=["decided X"],
            outcome="success",
        )
        db.upsert_session_digest(digest)
        ctx = build_context(db, settings)
        assert "<mem-context>" in ctx
        assert "## 過去セッション: proj" in ctx
        assert "past work summary" in ctx

    def test_digest_excluded_when_session_in_hot_layer(self, db: Database, settings: Settings) -> None:
        """hot 層に採用された session_id の digest は重複注入を避けるため除外される。"""
        now = _now()
        db.store_chunk(
            MemoryChunk(
                session_id="dup-session",
                project="proj",
                chunk_index=0,
                content="hot content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="hot prompt",
                created_at_epoch=now,
            )
        )
        db.upsert_session_digest(
            _make_digest(
                session_id="dup-session",
                summary="should be excluded",
                started_at_epoch=now - 100,
                created_at_epoch=now - 50,
            )
        )
        ctx = build_context(db, settings)
        assert "hot prompt" in ctx
        assert "should be excluded" not in ctx
        assert "## 過去セッション:" not in ctx

    def test_digest_zero_results_hot_only_ok(self, db: Database, settings: Settings) -> None:
        """digest が0件でも hot 層のみで正常に動作する。"""
        now = _now()
        db.store_chunk(
            MemoryChunk(
                session_id="s1",
                project="proj",
                chunk_index=0,
                content="hot only",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=now,
            )
        )
        ctx = build_context(db, settings)
        assert "hot only" in ctx
        assert "## 過去セッション:" not in ctx

    def test_digest_budget_truncation(self, db: Database) -> None:
        """digest_tokens を超えるダイジェストは打ち切られる。"""
        small_settings = Settings(context_chunk_count=50, context_digest_tokens=1)
        db.upsert_session_digest(
            _make_digest(
                session_id="d1",
                summary="x" * 500,
                started_at_epoch=1700000000,
                created_at_epoch=1700000000,
            )
        )
        ctx = build_context(db, small_settings)
        assert ctx == ""

    def test_digest_project_filter(self, db: Database, settings: Settings) -> None:
        """project 指定時は digest 層も対象プロジェクトのみに絞られる。"""
        db.upsert_session_digest(
            _make_digest(
                session_id="d-a",
                project="proj-a",
                summary="summary a",
                started_at_epoch=1700000000,
                created_at_epoch=1700000000,
            )
        )
        db.upsert_session_digest(
            _make_digest(
                session_id="d-b",
                project="proj-b",
                summary="summary b",
                started_at_epoch=1700000001,
                created_at_epoch=1700000001,
            )
        )
        ctx = build_context(db, settings, project="proj-a")
        assert "summary a" in ctx
        assert "summary b" not in ctx

    def test_output_order_digest_before_hot(self, db: Database, settings: Settings) -> None:
        """出力順は digest 層（古→新）→ hot 層。"""
        now = _now()
        db.store_chunk(
            MemoryChunk(
                session_id="hot-session",
                project="proj",
                chunk_index=0,
                content="hot content",
                tool_names=[],
                files_read=[],
                files_modified=[],
                user_prompt="",
                created_at_epoch=now,
            )
        )
        db.upsert_session_digest(
            _make_digest(
                session_id="past-session",
                summary="past summary",
                started_at_epoch=1700000000,
                created_at_epoch=1700000000,
            )
        )
        ctx = build_context(db, settings)
        digest_pos = ctx.index("## 過去セッション:")
        hot_pos = ctx.index("## セッション:")
        assert digest_pos < hot_pos

    def test_digest_order_oldest_to_newest(self, db: Database, settings: Settings) -> None:
        """複数 digest は created_at_epoch 昇順（古→新）で並ぶ。"""
        db.upsert_session_digest(
            _make_digest(
                session_id="newer",
                summary="newer summary",
                started_at_epoch=1700000200,
                created_at_epoch=1700000200,
            )
        )
        db.upsert_session_digest(
            _make_digest(
                session_id="older",
                summary="older summary",
                started_at_epoch=1700000100,
                created_at_epoch=1700000100,
            )
        )
        ctx = build_context(db, settings)
        older_pos = ctx.index("older summary")
        newer_pos = ctx.index("newer summary")
        assert older_pos < newer_pos


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

    def test_hot_token_budget_limits_output(self, db: Database) -> None:
        """hot_tokens が極小の場合、hot 層は空になる（digest も無ければ全体が空）。"""
        now = _now()
        small_settings = Settings(context_hot_tokens=1, context_chunk_count=50)
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
                    created_at_epoch=now + i,
                )
            )
        ctx = build_context(db, small_settings)
        assert ctx == "" or len(ctx) < 500


class TestFormatTimestamp:
    def test_format(self) -> None:
        result = _format_timestamp(1700000000)
        assert "2023-11-14" in result

    def test_utc(self) -> None:
        result = _format_timestamp(0)
        assert "1970-01-01 00:00" == result


class TestFormatDate:
    """_format_date のテスト"""

    def test_format(self) -> None:
        assert _format_date(1700000000) == "2023-11-14"

    def test_epoch_zero(self) -> None:
        assert _format_date(0) == "1970-01-01"


class TestFormatDigest:
    """_format_digest のテスト"""

    def test_full_fields(self) -> None:
        digest = _make_digest(
            session_id="s1",
            project="myproj",
            summary="did great work",
            started_at_epoch=1700000000,
            created_at_epoch=1700000100,
            key_files=["a.py", "b.py", "c.py", "d.py"],
            key_decisions=["decision 1", "decision 2", "decision 3"],
            outcome="partial",
        )
        rendered = _format_digest(digest)
        assert "## 過去セッション: myproj (2023-11-14) [partial]" in rendered
        assert "**要約**: did great work" in rendered
        assert "**変更**: a.py, b.py, c.py" in rendered
        assert "d.py" not in rendered
        assert "**論点**: decision 1 / decision 2" in rendered
        assert "decision 3" not in rendered

    def test_empty_key_files_line_omitted(self) -> None:
        digest = _make_digest(
            session_id="s1",
            summary="summary only",
            started_at_epoch=1700000000,
            created_at_epoch=1700000000,
            key_files=[],
            key_decisions=["only decision"],
        )
        rendered = _format_digest(digest)
        assert "**変更**:" not in rendered
        assert "**論点**: only decision" in rendered

    def test_empty_key_decisions_line_omitted(self) -> None:
        digest = _make_digest(
            session_id="s1",
            summary="summary only",
            started_at_epoch=1700000000,
            created_at_epoch=1700000000,
            key_files=["a.py"],
            key_decisions=[],
        )
        rendered = _format_digest(digest)
        assert "**変更**: a.py" in rendered
        assert "**論点**:" not in rendered

    def test_both_empty(self) -> None:
        digest = _make_digest(
            session_id="s1",
            summary="bare summary",
            started_at_epoch=1700000000,
            created_at_epoch=1700000000,
            key_files=[],
            key_decisions=[],
        )
        rendered = _format_digest(digest)
        assert "**変更**:" not in rendered
        assert "**論点**:" not in rendered
        assert "**要約**: bare summary" in rendered


class TestFilterHotChunks:
    """_filter_hot_chunks のテスト"""

    def _chunk(self, epoch: int) -> MemoryChunk:
        return MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=0,
            content="c",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=epoch,
        )

    def test_excludes_older_than_cutoff(self) -> None:
        recent = self._chunk(1000)
        old = self._chunk(500)
        result = _filter_hot_chunks([(recent, 0.5), (old, 0.9)], hot_cutoff=800)
        assert result == [(recent, 0.5)]

    def test_sorted_by_score_descending(self) -> None:
        a = self._chunk(1000)
        b = self._chunk(1001)
        result = _filter_hot_chunks([(a, 0.2), (b, 0.9)], hot_cutoff=0)
        assert result == [(b, 0.9), (a, 0.2)]


class TestSelectDigestsWithinBudget:
    """_select_digests_within_budget のテスト"""

    def test_stops_when_next_digest_does_not_fit(self) -> None:
        digest_a = _make_digest(
            session_id="a", summary="fit", started_at_epoch=1, created_at_epoch=1
        )
        digest_b = _make_digest(
            session_id="b", summary="x" * 2000, started_at_epoch=2, created_at_epoch=2
        )
        # digest_a はギリギリ収まるが digest_b は収まらない予算に設定する
        budget_chars = len(_format_digest(digest_a)) + 1
        max_tokens = budget_chars / 3.5
        selected = _select_digests_within_budget([digest_a, digest_b], max_tokens=max_tokens)
        assert selected == [digest_a]

    def test_oversized_leading_digest_does_not_drop_smaller_followers(self) -> None:
        """先頭の大きい digest が予算超過でも、後続の収まる digest は選択される。"""
        big = _make_digest(
            session_id="big", summary="x" * 2000, started_at_epoch=1, created_at_epoch=1
        )
        small = _make_digest(
            session_id="small", summary="fit", started_at_epoch=2, created_at_epoch=2
        )
        budget_chars = len(_format_digest(small)) + 1
        max_tokens = budget_chars / 3.5
        selected = _select_digests_within_budget([big, small], max_tokens=max_tokens)
        assert selected == [small]

    def test_empty_list(self) -> None:
        assert _select_digests_within_budget([], max_tokens=800) == []


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

    def test_oversized_leading_chunk_does_not_drop_smaller_followers(self) -> None:
        """先頭の大きいチャンクが予算超過でも、後続の収まるチャンクは選択される。"""
        big = MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=0,
            content="x" * 100,
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=1700000000,
        )
        small = MemoryChunk(
            session_id="s1",
            project="proj",
            chunk_index=1,
            content="fit",
            tool_names=[],
            files_read=[],
            files_modified=[],
            user_prompt="",
            created_at_epoch=1700000001,
        )

        selected = _select_within_budget([(big, 1.0), (small, 0.5)], max_tokens=4)

        assert selected == [small]


def test_format_chunk_limits_files_modified_to_two() -> None:
    """_format_chunk の変更ファイル表示は search 側 format_fields と同じ 2 件上限。"""
    from bluecore.mem.context import _format_chunk

    chunk = MemoryChunk(
        session_id="s1",
        project="proj",
        chunk_index=0,
        content="",
        tool_names=[],
        files_read=[],
        files_modified=["a.py", "b.py", "c.py"],
        user_prompt="",
        created_at_epoch=1700000000,
    )

    rendered = _format_chunk(chunk)

    assert "**変更ファイル**: a.py, b.py" in rendered
    assert "c.py" not in rendered
