"""cli_session_handlers の自動圧縮・手動圧縮・セッション終了・検索注入の分岐テスト。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bluecore.mem.search as search_mod
from bluecore.mem.cli_search_handlers import _format_digest_entry, render_digest_context
from bluecore.mem.cli_session_handlers import (
    SessionEndDeps,
    _auto_compact_if_needed,
    _search_and_inject_context,
    handle_compact,
    handle_session_end,
)
from bluecore.mem.models import MemoryChunk, SessionDigest
from bluecore.mem.search import DigestSearchResult, SearchResult
from tests.mem.conftest import FakeDB, make_settings, open_fake_db

_LOG = SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, info=lambda *a, **k: None)


def _make_digest(session_id: str = "digest-session", project: str = "proj", summary: str = "digest summary") -> SessionDigest:
    """テスト用の SessionDigest を構築する。"""
    return SessionDigest(
        session_id=session_id,
        project=project,
        summary=summary,
        started_at_epoch=1700000000,
        created_at_epoch=1700000000,
    )


def _make_search_result(chunk_id: str = "c1", content: str = "chunk content", score: float = 0.9) -> SearchResult:
    """テスト用の SearchResult を構築する。"""
    return SearchResult(
        chunk_id=chunk_id,
        score=score,
        content=content,
        project="proj",
        created_at_epoch=1700000000,
        tool_names=[],
        files_read=[],
        files_modified=[],
    )


def _extract_additional_context(out: str) -> str:
    """print() で出力された JSON から additionalContext を取り出す。"""
    payload = json.loads(out)
    return payload["hookSpecificOutput"]["additionalContext"]


def _make_chunk(session_id: str = "sess-1", chunk_id: str = "c1") -> MemoryChunk:
    """テスト用の MemoryChunk を構築する。"""
    return MemoryChunk(
        id=chunk_id,
        session_id=session_id,
        project="repo",
        chunk_index=0,
        content="content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=["a.py"],
        created_at_epoch=1704067200,
    )


def test_auto_compact_interval_not_elapsed(tmp_path: Path) -> None:
    """圧縮インターバル未経過なら何もしない。"""
    settings = make_settings(tmp_path)
    settings.auto_compact_interval_days = 1
    settings.last_compacted_at = 1000
    tm = SimpleNamespace(time=lambda: 1001)  # 経過 1s < 86400
    _auto_compact_if_needed(FakeDB(), settings, log=_LOG, time_module=tm)


def test_auto_compact_no_low_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """低品質チャンクが無ければ削除をスキップして最適化のみ行う。"""
    monkeypatch.setattr("bluecore.mem.compaction.detect_low_quality", lambda db: [])
    monkeypatch.setattr("bluecore.mem.compaction.optimize_db", lambda db: {})
    settings = make_settings(tmp_path)  # interval_days=0 → 経過判定を通過
    tm = SimpleNamespace(time=lambda: 100)
    _auto_compact_if_needed(FakeDB(), settings, log=_LOG, time_module=tm)


def test_handle_compact_no_low_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """手動圧縮で低品質チャンクが無ければ削除せず最適化のみ行う。"""
    monkeypatch.setattr("bluecore.mem.compaction.detect_low_quality", lambda db: [])
    monkeypatch.setattr("bluecore.mem.compaction.optimize_db", lambda db: {"fragmentation_before": 0.0})
    handle_compact(make_settings(tmp_path), open_db=lambda s: open_fake_db(FakeDB()), log=_LOG)
    assert "削除候補: 0 件" in capsys.readouterr().out


def _make_deps(db: FakeDB) -> SessionEndDeps:
    """テスト用の SessionEndDeps を組み立てる（埋め込みは固定値を返す）。"""
    return SessionEndDeps(
        open_db=lambda settings: open_fake_db(db),
        embed_fn=lambda texts: [[0.1, 0.2] for _ in texts],
        log=_LOG,
        time_module=SimpleNamespace(time=lambda: 100.0),
    )


class TestHandleSessionEndG3AndDigest:
    """G3 修正（end_session 常時呼び出し）とセッション要約生成のテスト。"""

    def test_end_session_called_even_with_zero_chunks(self, tmp_path: Path) -> None:
        """チャンクがゼロでも end_session が呼ばれ、終了時刻が記録される。"""
        db = FakeDB([])
        settings = make_settings(tmp_path, auto_compact_enabled=False)
        handle_session_end(settings, {"session_id": "sess-empty"}, _make_deps(db))
        assert db.ended_sessions == ["sess-empty"]
        # チャンクが無いので後続処理（埋め込み・digest）は実行されない
        assert db.embeddings == []
        assert db.digests == []

    def test_digest_is_stored_when_chunks_exist(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """チャンクが存在する場合、learn 同期後に digest が生成・保存される。"""
        chunk = _make_chunk()
        db = FakeDB([chunk])
        settings = make_settings(tmp_path, auto_compact_enabled=False)

        handle_session_end(settings, {"session_id": "sess-1"}, _make_deps(db))

        assert db.ended_sessions == ["sess-1"]
        assert len(db.digests) == 1
        assert db.digests[0].session_id == "sess-1"

    def test_digest_generation_exception_logs_warning_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """digest 生成が例外を投げても warning のみでフローは継続する。"""
        import bluecore.mem.digest as digest_mod

        chunk = _make_chunk()
        db = FakeDB([chunk])
        settings = make_settings(tmp_path, auto_compact_enabled=False)
        monkeypatch.setattr(
            digest_mod,
            "generate_and_store_digest",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("digest boom")),
        )

        warnings: list[str] = []
        log = SimpleNamespace(
            warning=lambda msg, *a, **k: warnings.append(msg % a if a else msg),
            info=lambda *a, **k: None,
            error=lambda *a, **k: None,
        )
        deps = SessionEndDeps(
            open_db=lambda settings: open_fake_db(db),
            embed_fn=lambda texts: [[0.1, 0.2] for _ in texts],
            log=log,
            time_module=SimpleNamespace(time=lambda: 100.0),
        )

        handle_session_end(settings, {"session_id": "sess-1"}, deps)

        assert any("digest 生成失敗" in warning for warning in warnings)
        # digest 失敗後も embedding 保存（前段）は完了している
        assert db.embeddings == [(["c1"], [[0.1, 0.2]])]


class TestSearchAndInjectContextDigestFirst:
    """_search_and_inject_context の digest 優先2段検索テスト。"""

    def _base_settings(self, tmp_path: Path):
        """テスト用の Settings インスタンスを返す。"""
        return make_settings(tmp_path)

    def test_digest_hit_appears_before_chunk_context(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """digest がヒットした場合、注入テキストの先頭に digest コンテキストが入る。"""
        chunk = MemoryChunk(
            id="c1",
            session_id="other-session",
            project="proj",
            chunk_index=0,
            content="chunk content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        )
        db = FakeDB([chunk])
        digest = _make_digest(session_id="digest-session")

        monkeypatch.setattr(
            search_mod.SearchService, "search_digests",
            lambda self, *a, **k: [DigestSearchResult(digest=digest, score=1.0)],
        )
        monkeypatch.setattr(
            search_mod.SearchService, "search",
            lambda self, **k: [_make_search_result(chunk_id="c1", content="chunk content")],
        )

        _search_and_inject_context(db, self._base_settings(tmp_path), "prompt", "proj", log=_LOG)
        ctx = _extract_additional_context(capsys.readouterr().out)

        assert "digest summary" in ctx
        assert "chunk content" in ctx
        assert ctx.index("digest summary") < ctx.index("chunk content")

    def test_digest_and_chunk_blocks_separated_by_newline(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """digest・chunk 双方非空のとき `</mem-context><mem-context>` が隣接せず改行区切りになる（Minor 修正）。"""
        chunk = MemoryChunk(
            id="c1",
            session_id="other-session",
            project="proj",
            chunk_index=0,
            content="chunk content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        )
        db = FakeDB([chunk])
        digest = _make_digest(session_id="digest-session")

        monkeypatch.setattr(
            search_mod.SearchService, "search_digests",
            lambda self, *a, **k: [DigestSearchResult(digest=digest, score=1.0)],
        )
        monkeypatch.setattr(
            search_mod.SearchService, "search",
            lambda self, **k: [_make_search_result(chunk_id="c1", content="chunk content")],
        )

        _search_and_inject_context(db, self._base_settings(tmp_path), "prompt", "proj", log=_LOG)
        ctx = _extract_additional_context(capsys.readouterr().out)

        assert "</mem-context><mem-context>" not in ctx
        assert "</mem-context>\n<mem-context>" in ctx

    def test_chunk_from_digest_session_excluded(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """digest ヒット済みセッションのチャンクは chunk 検索結果から除外される。"""
        chunk_same = MemoryChunk(
            id="c1",
            session_id="dup-session",
            project="proj",
            chunk_index=0,
            content="dup content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        )
        chunk_other = MemoryChunk(
            id="c2",
            session_id="other-session",
            project="proj",
            chunk_index=0,
            content="other content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        )
        db = FakeDB([chunk_same, chunk_other])
        digest = _make_digest(session_id="dup-session")

        monkeypatch.setattr(
            search_mod.SearchService, "search_digests",
            lambda self, *a, **k: [DigestSearchResult(digest=digest, score=1.0)],
        )
        monkeypatch.setattr(
            search_mod.SearchService, "search",
            lambda self, **k: [
                _make_search_result(chunk_id="c1", content="dup content", score=0.9),
                _make_search_result(chunk_id="c2", content="other content", score=0.5),
            ],
        )

        _search_and_inject_context(db, self._base_settings(tmp_path), "prompt", "proj", log=_LOG)
        ctx = _extract_additional_context(capsys.readouterr().out)

        assert "digest summary" in ctx
        assert "other content" in ctx
        assert "dup content" not in ctx

    def test_no_digest_hit_falls_back_to_chunk_only_output(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """digest 0件のとき、注入テキストは chunk 検索結果のみになる（見出しは digest 無し）。"""
        chunk = MemoryChunk(
            id="c1",
            session_id="s1",
            project="proj",
            chunk_index=0,
            content="chunk only content",
            tool_names=[],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        )
        db = FakeDB([chunk])

        monkeypatch.setattr(search_mod.SearchService, "search_digests", lambda self, *a, **k: [])
        monkeypatch.setattr(
            search_mod.SearchService, "search",
            lambda self, **k: [_make_search_result(chunk_id="c1", content="chunk only content")],
        )

        _search_and_inject_context(db, self._base_settings(tmp_path), "prompt", "proj", log=_LOG)
        ctx = _extract_additional_context(capsys.readouterr().out)

        assert "chunk only content" in ctx
        assert "過去セッション" not in ctx

    def test_no_results_at_all_prints_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """digest・chunk とも0件なら何も出力しない。"""
        db = FakeDB()
        monkeypatch.setattr(search_mod.SearchService, "search_digests", lambda self, *a, **k: [])
        monkeypatch.setattr(search_mod.SearchService, "search", lambda self, **k: [])

        _search_and_inject_context(db, self._base_settings(tmp_path), "prompt", "proj", log=_LOG)
        assert capsys.readouterr().out == ""


class TestRenderDigestContext:
    """render_digest_context のテスト（配置: cli_search_handlers.py の新設関数）。"""

    def test_empty_list_returns_empty_string(self) -> None:
        assert render_digest_context([]) == ""

    def test_single_result_rendered(self) -> None:
        digest = _make_digest(project="myproj", summary="did great work")
        rendered = render_digest_context([DigestSearchResult(digest=digest, score=1.0)])
        assert "<mem-context>" in rendered
        assert "</mem-context>" in rendered
        assert "myproj" in rendered
        assert "did great work" in rendered

    def test_budget_truncation_skips_oversized_entries(self) -> None:
        """max_tokens を超えるエントリは打ち切られ、収まらない場合は空文字。"""
        digest = _make_digest(summary="x" * 2000)
        rendered = render_digest_context([DigestSearchResult(digest=digest, score=1.0)], max_tokens=1)
        assert rendered == ""

    def test_oversized_leading_entry_does_not_drop_smaller_followers(self) -> None:
        """先頭が予算超過でも、後続の収まるエントリは選択される。"""
        big = _make_digest(session_id="big", summary="x" * 2000)
        small = _make_digest(session_id="small", summary="fit")
        small_entry_len = len(_format_digest_entry(small))
        max_tokens = (small_entry_len + 1) / 3.5

        rendered = render_digest_context(
            [DigestSearchResult(digest=big, score=1.0), DigestSearchResult(digest=small, score=0.5)],
            max_tokens=max_tokens,
        )
        assert "fit" in rendered
        assert "x" * 2000 not in rendered
