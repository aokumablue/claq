"""cli_session_handlers の自動圧縮・手動圧縮の分岐テスト。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from deepblue.mem.cli_session_handlers import _auto_compact_if_needed, handle_compact
from tests.mem.conftest import FakeDB, make_settings, open_fake_db

_LOG = SimpleNamespace(warning=lambda *a, **k: None, error=lambda *a, **k: None, info=lambda *a, **k: None)


def test_auto_compact_interval_not_elapsed(tmp_path: Path) -> None:
    """圧縮インターバル未経過なら何もしない。"""
    settings = make_settings(tmp_path)
    settings.auto_compact_interval_days = 1
    settings.last_compacted_at = 1000
    tm = SimpleNamespace(time=lambda: 1001)  # 経過 1s < 86400
    _auto_compact_if_needed(FakeDB(), settings, log=_LOG, time_module=tm)


def test_auto_compact_no_low_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """低品質チャンクが無ければ削除をスキップして最適化のみ行う。"""
    monkeypatch.setattr("deepblue.mem.compaction.detect_low_quality", lambda db: [])
    monkeypatch.setattr("deepblue.mem.compaction.optimize_db", lambda db: {})
    settings = make_settings(tmp_path)  # interval_days=0 → 経過判定を通過
    tm = SimpleNamespace(time=lambda: 100)
    _auto_compact_if_needed(FakeDB(), settings, log=_LOG, time_module=tm)


def test_handle_compact_no_low_quality(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """手動圧縮で低品質チャンクが無ければ削除せず最適化のみ行う。"""
    monkeypatch.setattr("deepblue.mem.compaction.detect_low_quality", lambda db: [])
    monkeypatch.setattr("deepblue.mem.compaction.optimize_db", lambda db: {"fragmentation_before": 0.0})
    handle_compact(make_settings(tmp_path), open_db=lambda s: open_fake_db(FakeDB()), log=_LOG)
    assert "削除候補: 0 件" in capsys.readouterr().out
