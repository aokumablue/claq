"""team: CLI ハンドラ (team-context / team-session-init) のテスト。"""

from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

import pytest

import devgear.mem.search as search_mod
from devgear.mem import cli as cli_module
from devgear.mem.settings import Settings, SyncSettings, TeamSettings


class _FakePg:
    def __init__(self, *, connected: bool = True) -> None:
        self.connected = connected
        self.closed = False

    def test_connection(self) -> bool:
        return self.connected

    def close(self) -> None:
        self.closed = True


def _settings(*, sync_enabled: bool = True, url: str = "postgres://x") -> Settings:
    s = Settings()
    s.sync = SyncSettings(enabled=sync_enabled, postgres_url=url)
    s.team = TeamSettings(enabled=True, max_tokens=1000, chunk_limit=5, exclude_self=True)
    return s


def test_team_context_skipped_when_sync_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = {"ok": False}

    def _fake_build(*a, **kw):  # noqa: ANN001, ANN002, ANN003
        called["ok"] = True
        return "<team-context>X</team-context>"

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _fake_build)
    assert cli_module._handle_team_context(_settings(sync_enabled=False), {"cwd": "/p/proj"}) == ""
    assert capsys.readouterr().out == ""
    assert called["ok"] is False


def test_team_context_skipped_when_url_empty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert cli_module._handle_team_context(_settings(url=""), {"cwd": "/p/proj"}) == ""
    assert capsys.readouterr().out == ""


def test_team_context_skipped_when_team_disabled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    s = _settings()
    s.team = TeamSettings(enabled=False)
    assert cli_module._handle_team_context(s, {"cwd": "/p/proj"}) == ""
    assert capsys.readouterr().out == ""


def test_team_context_silent_on_connection_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase",
        lambda url: _FakePg(connected=False),
    )
    assert cli_module._handle_team_context(_settings(), {"cwd": "/p/proj"}) == ""
    assert capsys.readouterr().out == ""


def test_team_context_prints_additional_context(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pg_instance = _FakePg(connected=True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )

    captured_kwargs: dict = {}

    def _fake_build(pg, *, query, exclude_origin_user, settings, mode, embedding_model=None):  # noqa: ANN001
        captured_kwargs.update(
            {
                "query": query,
                "exclude": exclude_origin_user,
                "mode": mode,
                "embedding_model": embedding_model,
            }
        )
        return "<team-context>hello</team-context>"

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _fake_build)

    result = cli_module._handle_team_context(_settings(), {"cwd": "/home/u/x-picflow"})

    assert "<team-context>" in result
    assert capsys.readouterr().out == ""
    assert captured_kwargs["query"] == "x-picflow"
    assert captured_kwargs["exclude"] == "me"
    assert captured_kwargs["mode"] == "fts"
    assert pg_instance.closed is True


def test_team_context_respects_excluded_projects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _settings()
    s.excluded_projects = ["x-picflow"]
    assert cli_module._handle_team_context(s, {"cwd": "/home/u/x-picflow"}) == ""
    assert capsys.readouterr().out == ""


def test_team_session_init_requires_retrospective_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    called = {"ok": False}
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase",
        lambda url: (_ for _ in ()).throw(AssertionError("should not reach PG")),
    )
    monkeypatch.setattr(
        "devgear.mem.team_context.build_team_context",
        lambda *a, **kw: called.__setitem__("ok", True) or "",  # noqa: ANN001
    )

    cli_module._handle_team_session_init(
        _settings(), {"cwd": "/p/proj", "prompt": "新機能を追加してください"}
    )

    assert capsys.readouterr().out == ""
    assert called["ok"] is False


def test_team_session_init_fires_on_retrospective_keyword(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pg_instance = _FakePg(connected=True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )

    captured: dict = {}

    def _fake_build(pg, *, query, exclude_origin_user, settings, mode, embedding_model=None):  # noqa: ANN001
        captured.update(
            {
                "query": query,
                "exclude": exclude_origin_user,
                "mode": mode,
                "embedding_model": embedding_model,
            }
        )
        return "<team-context>history</team-context>"

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _fake_build)

    cli_module._handle_team_session_init(
        _settings(), {"cwd": "/home/u/x-picflow", "prompt": "前回どう直した？"}
    )

    assert capsys.readouterr().out.strip().startswith('{"hookEventName"')
    assert captured["mode"] == "hybrid"
    assert captured["exclude"] == "me"
    assert "前回どう直した？" in captured["query"]
    assert "x-picflow" in captured["query"]
    assert captured["embedding_model"]  # settings のモデル名が渡る


def test_team_session_init_silent_on_empty_prompt(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module._handle_team_session_init(_settings(), {"cwd": "/p/proj", "prompt": ""})
    assert capsys.readouterr().out == ""


def test_team_session_init_respects_exclude_self_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    s = _settings()
    s.team = TeamSettings(enabled=True, exclude_self=False)
    pg_instance = _FakePg(connected=True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )

    captured: dict = {}

    def _fake_build(pg, *, query, exclude_origin_user, settings, mode, embedding_model=None):  # noqa: ANN001
        captured["exclude"] = exclude_origin_user
        return "<team-context>x</team-context>"

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _fake_build)
    cli_module._handle_team_session_init(
        s, {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
    )
    assert captured["exclude"] == ""


def test_team_context_and_session_init_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = _settings()
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: True)

    assert cli_module._handle_team_context(settings, {"cwd": "/"}) == ""
    assert capsys.readouterr().out == ""

    fake_pg = ModuleType("devgear.mem.pg_database")
    fake_team = ModuleType("devgear.mem.team_context")
    monkeypatch.setitem(sys.modules, "devgear.mem.pg_database", fake_pg)
    monkeypatch.setitem(sys.modules, "devgear.mem.team_context", fake_team)

    assert cli_module._handle_team_context(settings, {"cwd": "/home/u/x-picflow"}) == ""
    assert capsys.readouterr().out == ""

    fake_pg.PgDatabase = lambda url: SimpleNamespace(
        test_connection=lambda: False,
        close=lambda: None,
    )
    fake_team.build_team_context = lambda *args, **kwargs: None
    assert cli_module._handle_team_context(settings, {"cwd": "/home/u/x-picflow"}) == ""
    assert capsys.readouterr().out == ""

    fake_pg.PgDatabase = lambda url: SimpleNamespace(
        test_connection=lambda: True,
        close=lambda: None,
    )
    fake_team.build_team_context = lambda *args, **kwargs: None
    assert cli_module._handle_team_session_init(
        settings, {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
    ) is None
    assert capsys.readouterr().out == ""

    fake_pg.PgDatabase = lambda url: SimpleNamespace(
        test_connection=lambda: True,
        close=lambda: None,
    )
    fake_team.build_team_context = lambda *args, **kwargs: None
    assert cli_module._handle_team_session_init(
        settings, {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
    ) is None
    assert capsys.readouterr().out == ""


def test_team_context_build_raises_and_logs_warning(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """handle_team_context 内で build_team_context が例外を出した場合のログを通す。"""
    pg_instance = _FakePg(connected=True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("build boom")

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _boom)

    result = cli_module._handle_team_context(_settings(), {"cwd": "/home/u/x-picflow"})
    assert result == ""
    assert pg_instance.closed is True


def test_team_session_init_disabled_returns_early(capsys: pytest.CaptureFixture[str]) -> None:
    """sync 無効/未設定なら何もせず None を返す（line 73）。"""
    assert (
        cli_module._handle_team_session_init(
            _settings(sync_enabled=False), {"cwd": "/p", "prompt": "前回の話"}
        )
        is None
    )
    assert capsys.readouterr().out == ""


def test_team_session_init_skips_excluded_project(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """excluded_projects に含まれるプロジェクトでは早期 return（line 81）。"""
    s = _settings()
    s.excluded_projects = ["x-picflow"]
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: True)
    assert (
        cli_module._handle_team_session_init(
            s, {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
        )
        is None
    )
    assert capsys.readouterr().out == ""


def test_team_session_init_import_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """team-session-init モジュール読み込み失敗（lines 89-91）。"""
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")

    # pg_database のロードを実際に失敗させる
    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def _bad_import(name, *args, **kwargs):
        if name == "devgear.mem.pg_database":
            raise ImportError("simulated import failure")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _bad_import)
    assert (
        cli_module._handle_team_session_init(
            _settings(), {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
        )
        is None
    )


def test_team_session_init_test_connection_false(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """test_connection が False の場合（lines 96-97）。"""
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    pg_instance = _FakePg(connected=False)
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )
    assert (
        cli_module._handle_team_session_init(
            _settings(), {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
        )
        is None
    )
    assert pg_instance.closed is True


def test_team_session_init_build_raises(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """build_team_context が例外を出した場合（lines 116-117）。"""
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: True)
    monkeypatch.setattr(cli_module, "get_git_user_name", lambda: "me")
    pg_instance = _FakePg(connected=True)
    monkeypatch.setattr(
        "devgear.mem.pg_database.PgDatabase", lambda url: pg_instance
    )

    def _boom(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        raise RuntimeError("build boom")

    monkeypatch.setattr("devgear.mem.team_context.build_team_context", _boom)
    cli_module._handle_team_session_init(
        _settings(), {"cwd": "/home/u/x-picflow", "prompt": "前回の話"}
    )
    assert pg_instance.closed is True


def test_session_init_skips_when_prompt_not_inject(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """cli_session_handlers.handle_session_init: should_inject_memory が False で早期 return（line 76）。"""
    from tests.mem.conftest import FakeDB, make_settings

    settings = make_settings(tmp_path)
    db = FakeDB()
    monkeypatch.setattr(cli_module, "_open_db", lambda settings: db)
    monkeypatch.setattr(search_mod, "should_inject_memory", lambda prompt: False)

    cli_module._handle_session_init(
        settings, {"cwd": str(tmp_path), "session_id": "s1", "prompt": "通常のプロンプト"}
    )
    # search が呼ばれていないこと（FakeDB の sessions に upsert はされている）
    assert len(db.sessions) >= 1
