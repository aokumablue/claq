"""cli_dashboard_handlers.handle_import の取り込み分岐テスト。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import bluecore.mem.importers as imp
from bluecore.mem.cli_dashboard_handlers import handle_import
from tests.mem.conftest import FakeDB, make_settings, open_fake_db


def test_handle_import_all_types(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """instincts/adrs/events 全指定で各取り込みを実行する。"""
    monkeypatch.setattr(imp, "import_instincts", lambda db, u: 1)
    monkeypatch.setattr(imp, "import_adrs", lambda db, u, r: 2)
    monkeypatch.setattr(imp, "import_event_logs", lambda db, u: 3)
    settings = make_settings(tmp_path)
    handle_import(
        settings,
        {"types": ["instincts", "adrs", "events"], "repo_root": "/repo"},
        open_db=lambda s: open_fake_db(FakeDB()),
        get_git_user_name=lambda: "user",
    )
    result = json.loads(capsys.readouterr().out)["imported"]
    assert result == {"instincts": 1, "adrs": 2, "events": 3}


def test_handle_import_no_types(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """type 指定が空なら全取り込みをスキップする。"""
    settings = make_settings(tmp_path)
    handle_import(
        settings,
        {"types": []},
        open_db=lambda s: open_fake_db(FakeDB()),
        get_git_user_name=lambda: "user",
    )
    result = json.loads(capsys.readouterr().out)["imported"]
    assert result == {"instincts": 0, "adrs": 0, "events": 0}


def test_collect_pg_dashboard_data_fetch_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """パネルデータ取得が例外でも握りつぶして空データを返す。"""
    from types import SimpleNamespace

    import bluecore.mem.cli_dashboard_handlers as h
    from bluecore.mem.settings import Settings, SyncSettings

    fake_pg = SimpleNamespace(
        test_connection=lambda: True,
        _get_conn=lambda: object(),
        _put_conn=lambda c: None,
        close=lambda: None,
    )
    monkeypatch.setattr("bluecore.mem.pg_database.PgDatabase", lambda url: fake_pg)
    monkeypatch.setattr(
        h, "_fetch_pg_panel_data",
        lambda pg, conn, days: (_ for _ in ()).throw(RuntimeError("fetch fail")),
    )
    settings = Settings()
    settings.sync = SyncSettings(enabled=True, postgres_url="postgres://x")
    result = h._collect_pg_dashboard_data(settings, 7, log=SimpleNamespace(warning=lambda *a, **k: None))
    assert result[0] is False
