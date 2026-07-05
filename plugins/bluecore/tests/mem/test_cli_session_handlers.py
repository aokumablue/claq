"""cli_session_handlers の自動圧縮・手動圧縮・セッション終了の分岐テスト。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import bluecore.mem.bridge as bridge_mod
from bluecore.mem.cli_session_handlers import (
    SessionEndDeps,
    _auto_compact_if_needed,
    handle_compact,
    handle_session_end,
)
from bluecore.mem.models import MemoryChunk
from tests.mem.conftest import FakeDB, make_settings, open_fake_db

_LOG = SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, info=lambda *a, **k: None)


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
        user_prompt="prompt",
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
        monkeypatch.setattr(bridge_mod, "sync_session_to_observations", lambda db, session_id: 1)

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
        monkeypatch.setattr(bridge_mod, "sync_session_to_observations", lambda db, session_id: 1)
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
