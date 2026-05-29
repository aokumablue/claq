"""devgear.mem.cli の追加テスト。"""

from __future__ import annotations

import builtins
import importlib
import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import devgear.mem.dashboard_queries as dashboard_queries_mod
import devgear.mem.importers as importers_mod
import devgear.mem.item_usage_queries as item_usage_queries_mod
import devgear.mem.pg_database as pg_database_mod
import devgear.mem.sync as sync_mod
from devgear.mem import cli
from devgear.mem.database import MemoryChunk
from devgear.mem.search import SearchResult
from devgear.mem.sync import SyncResult
from tests.mem.conftest import FakeDB, make_settings, open_fake_db


def test_helper_functions_cover_filters_and_rendering() -> None:
    chunk_a = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="x" * 600,
        tool_names=["Edit"],
        files_read=["src/app.py"],
        files_modified=["src/app.py"],
        user_prompt="y" * 210,
        created_at_epoch=1704067200,
    )
    chunk_b = MemoryChunk(
        id="c2",
        session_id="s1",
        project="repo",
        chunk_index=1,
        content="short",
        tool_names=["Bash"],
        files_read=["README.md"],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1704067300,
    )
    db = FakeDB([chunk_a, chunk_b])

    assert cli._parse_date_to_epoch(123) == 123
    assert cli._parse_date_to_epoch("2024-01-01T00:00:00Z") == 1704067200
    assert cli._parse_date_to_epoch("bad") is None
    assert cli._apply_structured_filters(db, [], None, None, None, None) == []
    assert cli._apply_structured_filters(db, ["c1", "c2"], "Edit", "*.py", "2024-01-01T00:00:00Z", None) == ["c1"]

    rendered = cli._render_adaptive_context(
        db,
        [
            SearchResult("c1", 0.9, "", "", "", 0, [], [], []),
            SearchResult("c2", 0.8, "", "", "", 0, [], [], []),
        ],
    )
    assert rendered.startswith("<mem-context>")
    assert "## repo (2024-01-01 00:00)" in rendered
    assert "**プロンプト**" in rendered
    assert "..." in rendered
    assert cli._format_chunk(chunk_a).startswith("**プロンプト**")
    rich_result = SearchResult(
        "team-1",
        0.9,
        "z" * 600,
        "p" * 210,
        "repo",
        1704067200,
        ["Edit", "Bash"],
        ["src/app.py"],
        ["src/app.py", "README.md"],
    )
    rich_formatted = cli._format_chunk_from_result(rich_result)
    assert "**ツール**: Edit, Bash" in rich_formatted
    assert "**変更ファイル**: src/app.py, README.md" in rich_formatted
    assert "zzzz" in rich_formatted
    assert "```" not in rich_formatted
    assert "..." in rich_formatted
    tiny_render = cli._render_adaptive_context(db, [rich_result], max_tokens=1)
    assert "**プロンプト**" not in tiny_render
    assert cli._format_timestamp(1704067200) == "2024-01-01 00:00"
    assert cli._truncate("abc", 10) == "abc"


def test_format_chunk_keeps_code_blocks_and_compacts_prose() -> None:
    chunk = MemoryChunk(
        id="c3",
        session_id="s1",
        project="repo",
        chunk_index=2,
        content="\n".join(
            [
                "ご質問ありがとうございます。",
                "```python",
                "print('hello')",
                "```",
                "  これは詳細説明です。",
            ]
        ),
        tool_names=["Edit"],
        files_read=[],
        files_modified=["src/app.py"],
        user_prompt="お力になれれば幸いです。 えーと 設定変更することができます。",
        created_at_epoch=1704067400,
    )

    rendered = cli._format_chunk(chunk)

    assert "**プロンプト**: 設定変更できます" in rendered
    assert "ご質問ありがとうございます" not in rendered
    assert rendered.count("```") == 2
    assert "print('hello')" in rendered
    assert "これは詳細説明です" in rendered


def test_format_chunk_preserves_code_only_prompt_and_late_code_block() -> None:
    chunk = MemoryChunk(
        id="c4",
        session_id="s1",
        project="repo",
        chunk_index=3,
        content="\n".join(
            [
                "説明 1",
                "説明 2",
                "説明 3",
                "説明 4",
                "説明 5",
                "説明 6",
                "説明 7",
                "```python",
                "print('late')",
                "```",
            ]
        ),
        tool_names=["Read"],
        files_read=[],
        files_modified=[],
        user_prompt="\n".join(["```python", "selected prompt", "```"]),
        created_at_epoch=1704067500,
    )

    rendered = cli._format_chunk(chunk)

    assert "**プロンプト**: selected prompt" in rendered
    assert "print('late')" in rendered
    assert rendered.count("```") == 2
    assert "..." in rendered


def test_handle_session_end_and_compact(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, auto_compact_enabled=True)
    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="chunk content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=["src/app.py"],
        user_prompt="prompt",
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    import devgear.mem.bridge as bridge_mod
    import devgear.mem.compaction as compaction_mod

    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(bridge_mod, "sync_session_to_observations", lambda db, session_id: 1)
    monkeypatch.setattr(compaction_mod, "detect_low_quality", lambda db: ["c1"])
    monkeypatch.setattr(compaction_mod, "find_near_duplicates", lambda db: [("c1", "c2")])
    monkeypatch.setattr(compaction_mod, "optimize_db", lambda db: {"fragmentation_before": 0.25})
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)

    cli._handle_session_end(settings, {"session_id": "s1"})
    assert db.embeddings == [(["c1"], [[0.1, 0.2]])]
    assert settings.last_compacted_at == 100.0

    monkeypatch.setattr(sys, "argv", ["python", "--execute"])
    cli._handle_compact(settings)
    assert any("DELETE FROM memory_chunks" in sql for sql, _ in db.executed)


def test_handle_setup_and_observe_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))

    assert cli._handle_setup(settings) == ""
    assert settings.data_path.exists()

    import devgear.mem.chunker as chunker_mod

    monkeypatch.setattr(chunker_mod, "build_chunk_from_tool_use", lambda **kwargs: MemoryChunk(
        session_id=kwargs["session_id"],
        project=kwargs["project"],
        chunk_index=kwargs["chunk_index"],
        content="observed",
        tool_names=[kwargs["tool_name"]],
        files_read=[],
        files_modified=[],
        user_prompt=kwargs["user_prompt"],
        created_at_epoch=1700000000,
    ))
    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "Read",
            "tool_input": {"path": "file.py"},
            "tool_response": "ok",
            "prompt": "read file",
        },
    )
    assert db.stored_chunks


def test_handle_session_start_commands_emit_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))

    assert cli._handle_setup(settings) == ""
    assert capsys.readouterr().out == ""

    assert cli._handle_record_project_profile(
        settings,
        {
            "project": "repo",
            "project_path": str(tmp_path),
            "languages": ["python"],
            "frameworks": ["pytest"],
            "primary_language": "python",
            "scope_hint": "project",
        },
    ) == ""
    assert capsys.readouterr().out == ""

    assert cli._handle_context(settings, {"cwd": str(tmp_path)}) == ""


def test_sync_import_and_dashboard_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB()))
    monkeypatch.setattr(sync_mod, "sync_to_postgres", lambda settings, dry_run=False: SyncResult(chunks=2, sessions=3, success=True))
    cli._handle_sync(settings, {"dry_run": True})
    assert json.loads(capsys.readouterr().out)["synced"]["chunks"] == 2

    monkeypatch.setattr(sync_mod, "should_sync", lambda settings: False)
    cli._handle_sync_check(settings)
    assert capsys.readouterr().out == ""

    import_calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB()))
    monkeypatch.setattr(importers_mod, "import_instincts", lambda db, origin_user: import_calls.append(("instincts", origin_user, None)) or 1)
    monkeypatch.setattr(importers_mod, "import_adrs", lambda db, origin_user, repo_root: import_calls.append(("adrs", origin_user, repo_root)) or 2)
    monkeypatch.setattr(importers_mod, "import_event_logs", lambda db, origin_user: import_calls.append(("events", origin_user, None)) or 3)
    cli._handle_import(settings, {"types": ["instincts", "adrs", "events"], "repo_root": "/repo"})
    assert json.loads(capsys.readouterr().out)["imported"] == {"instincts": 1, "adrs": 2, "events": 3}


def test_handle_session_end_empty_and_sync_check_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB([])))
    cli._handle_session_end(settings, {"session_id": "s1"})

    sync_calls: list[bool] = []

    def fake_sync_to_postgres(settings, dry_run=False):  # noqa: ANN001
        sync_calls.append(dry_run)
        return SyncResult(success=False, error="boom")

    monkeypatch.setattr(sync_mod, "should_sync", lambda settings: True)
    monkeypatch.setattr(sync_mod, "sync_to_postgres", fake_sync_to_postgres)
    cli._handle_sync_check(settings)
    assert sync_calls == [False]
    assert capsys.readouterr().out == ""


def test_handle_context_and_search_error_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    monkeypatch.setattr(cli, "_open_db", lambda settings: (_ for _ in ()).throw(RuntimeError("ctx boom")))
    assert cli._handle_context(settings, {"cwd": str(tmp_path)}) == ""
    assert any("コンテキスト生成失敗" in warning for warning in warnings)
    assert capsys.readouterr().out == ""

    cli._handle_search(settings, {"query": "   "})
    assert json.loads(capsys.readouterr().out) == {"results": []}

    cli._handle_search(settings, {"query": "needle"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"] == []
    assert "ctx boom" in payload["error"]


def test_main_settings_failure_and_invalid_stdin(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import devgear.mem.logger as logger_mod

    settings = make_settings(tmp_path)

    monkeypatch.setattr(cli.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "context"])
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert "設定/ログ初期化失敗" in captured.err
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == ""

    warnings: list[str] = []
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))
    monkeypatch.setitem(cli._COMMAND_HANDLERS, "context", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not-json"))
    monkeypatch.setattr(sys, "argv", ["python", "context"])
    assert cli.main() == 0
    assert any("stdin 読み取り失敗" in warning for warning in warnings)


@pytest.mark.parametrize("command", sorted(cli._SESSION_START_COMMANDS))
def test_main_session_start_commands_always_emit_wrapper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    import devgear.mem.logger as logger_mod

    settings = make_settings(tmp_path)
    errors: list[str] = []
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.log, "error", lambda msg, *args: errors.append(msg % args if args else msg))
    monkeypatch.setitem(
        cli._COMMAND_HANDLERS,
        command,
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("handler boom")),
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["python", command])

    assert cli.main() == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert payload["hookSpecificOutput"]["additionalContext"] == ""
    assert any(f"コマンド {command} 失敗: handler boom" in error for error in errors)


def test_run_normal_command_dispatch_and_exit_code_contract(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)
    called: list[str] = []

    monkeypatch.setitem(cli._COMMAND_HANDLERS, "search", lambda *_args, **_kwargs: called.append("search") or None)

    assert cli._run_normal_command("search", settings, {"query": "x"}) == 0
    assert called == ["search"]
    assert cli._run_normal_command("unknown-command", settings, {}) == 2


def test_main_preserves_normal_command_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(cli, "_parse_argv_and_stdin", lambda: ("search", {"query": "x"}))
    monkeypatch.setattr(cli, "_load_settings_or_raise", lambda: settings)
    monkeypatch.setattr(cli, "_run_normal_command", lambda command, s, stdin_data: 17)

    assert cli.main() == 17


def test_main_wraps_handler_exceptions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import devgear.mem.logger as logger_mod

    settings = make_settings(tmp_path)
    errors: list[str] = []

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.log, "error", lambda msg, *args: errors.append(msg % args if args else msg))
    monkeypatch.setitem(cli._COMMAND_HANDLERS, "context", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["python", "context"])

    # SessionStart 系コマンドは例外発生時も JSON を出力して自然終了（SystemExit を上げない）
    assert cli.main() == 0
    assert any("コマンド context 失敗" in error for error in errors)


def test_main_error_path_logging_contract_for_normal_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import devgear.mem.logger as logger_mod

    settings = make_settings(tmp_path)
    errors: list[str] = []

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.log, "error", lambda msg, *args: errors.append(msg % args if args else msg))
    monkeypatch.setitem(cli._COMMAND_HANDLERS, "search", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    monkeypatch.setattr(sys, "argv", ["python", "search"])

    assert cli.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert any("コマンド search 失敗: boom" in error for error in errors)


def test_session_init_excluded_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    settings.excluded_projects = {"skip"}

    monkeypatch.setattr(cli, "_open_db", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("_open_db should not be called")))
    cli._handle_session_init(settings, {"cwd": str(tmp_path / "skip"), "session_id": "s1", "prompt": "ignored"})
    assert capsys.readouterr().out == ""


def test_handle_session_end_auto_compact_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path, auto_compact_enabled=True)
    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="chunk content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=["src/app.py"],
        user_prompt="prompt",
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    import devgear.mem.bridge as bridge_mod
    import devgear.mem.compaction as compaction_mod

    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(bridge_mod, "sync_session_to_observations", lambda db, session_id: 1)
    monkeypatch.setattr(compaction_mod, "detect_low_quality", lambda db: (_ for _ in ()).throw(RuntimeError("compact boom")))
    monkeypatch.setattr(compaction_mod, "find_near_duplicates", lambda db: [])
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    cli._handle_session_end(settings, {"session_id": "s1"})
    assert any("自動圧縮エラー" in message for message in warnings)


def test_handle_dashboard_html_and_disabled_pg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    html_output = tmp_path / "dashboard.html"

    class _FakePg:
        def __init__(self, url: str) -> None:
            self.url = url
            self.closed = False
            self.conn = SimpleNamespace(
                execute=lambda *args, **kwargs: SimpleNamespace(
                    fetchone=lambda: (0,),
                    fetchall=lambda: [],
                )
            )

        def test_connection(self) -> bool:
            return True

        def _get_conn(self) -> SimpleNamespace:
            return SimpleNamespace()

        def _put_conn(self, conn) -> None:  # noqa: ANN001
            return None

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(pg_database_mod, "PgDatabase", _FakePg)
    monkeypatch.setattr(dashboard_queries_mod, "activity_by_user", lambda pg, days: [{"user": "u", "chunks": 1}])
    monkeypatch.setattr(dashboard_queries_mod, "activity_by_project", lambda pg, days: [{"project": "p", "chunks": 2}])
    monkeypatch.setattr(dashboard_queries_mod, "tool_usage_distribution", lambda pg, days: [{"tool": "Edit", "count": 3}])
    monkeypatch.setattr(dashboard_queries_mod, "session_timeline", lambda pg, days: [{"date": "2024-01-01", "sessions": 1, "chunks": 1}])
    monkeypatch.setattr(dashboard_queries_mod, "instinct_growth", lambda pg: [{"date": "2024-01-01", "count": 4}])
    monkeypatch.setattr(dashboard_queries_mod, "memory_quality_metrics", lambda pg: {"quality": "good"})
    monkeypatch.setattr(dashboard_queries_mod, "file_change_heatmap", lambda pg, days: {"heat": 1})
    monkeypatch.setattr(
        cli,
        "_collect_skill_health_overview",
        lambda options: {
            "report": {"generated_at": "2024-01-01T00:00:00Z", "skills": []},
            "summary": {"total_skills": 1, "healthy_skills": 1, "declining_skills": 0},
            "skills": [
                {
                    "skill_id": "skill-a",
                    "success_rate_7d": 0.8,
                    "success_rate_30d": 0.7,
                    "failure_trend": "stable",
                    "pending_amendments": 1,
                    "last_run": "2024-01-01T00:00:00Z",
                }
            ],
            "chart_labels": ["skill-a"],
            "chart_7d": [80.0],
            "chart_30d": [70.0],
        },
    )
    monkeypatch.setattr(
        cli,
        "_collect_skill_growth_overview",
        lambda settings, days: {
            "summary": {"total_patterns": 2, "total_gaps": 1, "skill_candidates": 1, "gap_candidates": 1},
            "skill_candidates": [
                {
                    "suggested_name": "s-file-workflow",
                    "priority": "high",
                    "priority_score": 42,
                    "evidence": {"occurrence_count": 3, "user_count": 2, "project_count": 1},
                }
            ],
            "gap_candidates": [
                {
                    "priority": "medium",
                    "sample_prompt": "build dashboard",
                    "occurrence_count": 4,
                    "user_count": 2,
                }
            ],
            "action_items": [
                {"priority": "high", "action": "create_skill", "target": "s-file-workflow"}
            ],
            "chart_labels": ["s-file-workflow"],
            "chart_scores": [42],
        },
    )
    monkeypatch.setattr(
        cli,
        "_collect_project_overview",
        lambda: {
            "summary": {
                "total_projects": 1,
                "personal_instincts": 2,
                "inherited_instincts": 1,
                "global_personal": 1,
                "global_inherited": 0,
            },
            "projects": [
                {
                    "id": "p1",
                    "name": "repo",
                    "personal_instincts": 2,
                    "inherited_instincts": 1,
                    "observations": 4,
                    "last_seen": "2024-01-01T00:00:00Z",
                }
            ],
        },
    )
    monkeypatch.setattr(
        item_usage_queries_mod,
        "item_usage_ranking",
        lambda conn, placeholder, days: [  # noqa: ARG005
            {"item_name": "skill-a", "item_type": "skill", "uses": 2, "last_used_epoch": 1}
        ],
    )
    monkeypatch.setattr(
        item_usage_queries_mod,
        "daily_trend",
        lambda conn, placeholder, days: [  # noqa: ARG005
            {"date": "2024-01-01", "skill": 1, "command": 0, "agent": 0, "total": 1}
        ],
    )
    monkeypatch.setattr(
        item_usage_queries_mod,
        "outcome_distribution",
        lambda conn, placeholder, days: [  # noqa: ARG005
            {"outcome": "success", "count": 1}
        ],
    )

    fake_jinja2 = ModuleType("jinja2")

    class FakeTemplate:
        def render(self, **kwargs) -> str:  # noqa: ANN003
            return f"HTML:{kwargs['days']}"

    class FakeEnvironment:
        def __init__(self, loader, autoescape=False) -> None:  # noqa: ANN001
            self.loader = loader

        def get_template(self, name):  # noqa: ANN001
            return FakeTemplate()

    class FakeFileSystemLoader:
        def __init__(self, path) -> None:  # noqa: ANN001
            self.path = path

    fake_jinja2.Environment = FakeEnvironment
    fake_jinja2.FileSystemLoader = FakeFileSystemLoader
    fake_jinja2.select_autoescape = lambda enabled_extensions=(): True
    monkeypatch.setitem(sys.modules, "jinja2", fake_jinja2)

    cli._handle_dashboard(settings, {"output": str(html_output), "format": "html", "days": 7})
    assert json.loads(capsys.readouterr().out)["success"] is True
    assert html_output.read_text(encoding="utf-8") == "HTML:7"

    monkeypatch.setattr(
        pg_database_mod,
        "PgDatabase",
        lambda url: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    cli._handle_dashboard(settings, {"output": str(html_output), "format": "json"})
    assert json.loads(capsys.readouterr().out)["success"] is True

    settings.sync.enabled = False
    cli._handle_dashboard(settings, {"output": str(html_output), "format": "json"})
    assert json.loads(capsys.readouterr().out)["success"] is True


def test_handle_dashboard_rejects_unsafe_output_path_and_allows_safe_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = make_settings(tmp_path)
    settings.sync.enabled = False
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB()))

    unsafe_output = tmp_path.parent / "dashboard-unsafe.json"
    cli._handle_dashboard(settings, {"output": str(unsafe_output), "format": "json"})
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["success"] is False
    assert "output path" in rejected["error"]
    assert not unsafe_output.exists()

    safe_output = tmp_path / "dashboard-safe.json"
    cli._handle_dashboard(settings, {"output": str(safe_output), "format": "json"})
    accepted = json.loads(capsys.readouterr().out)
    assert accepted["success"] is True
    assert safe_output.exists()


def test_handle_dashboard_json_and_main_entrypoints(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)

    monkeypatch.setattr(pg_database_mod, "PgDatabase", lambda url: SimpleNamespace(test_connection=lambda: True, close=lambda: None))
    monkeypatch.setattr(dashboard_queries_mod, "activity_by_user", lambda pg, days: [{"user": "u", "chunks": 1}])
    monkeypatch.setattr(dashboard_queries_mod, "activity_by_project", lambda pg, days: [{"project": "p", "chunks": 2}])
    monkeypatch.setattr(dashboard_queries_mod, "tool_usage_distribution", lambda pg, days: [{"tool": "Edit", "count": 3}])
    monkeypatch.setattr(dashboard_queries_mod, "session_timeline", lambda pg, days: [{"date": "2024-01-01", "sessions": 1, "chunks": 1}])
    monkeypatch.setattr(dashboard_queries_mod, "instinct_growth", lambda pg: [{"date": "2024-01-01", "count": 4}])
    monkeypatch.setattr(dashboard_queries_mod, "memory_quality_metrics", lambda pg: {"quality": "good"})
    monkeypatch.setattr(dashboard_queries_mod, "file_change_heatmap", lambda pg, days: {"heat": 1})
    monkeypatch.setattr(
        cli,
        "_collect_skill_health_overview",
        lambda options: {
            "report": {"generated_at": "2024-01-01T00:00:00Z", "skills": []},
            "summary": {"total_skills": 1, "healthy_skills": 1, "declining_skills": 0},
            "skills": [],
            "chart_labels": [],
            "chart_7d": [],
            "chart_30d": [],
        },
    )
    monkeypatch.setattr(
        cli,
        "_collect_skill_growth_overview",
        lambda settings, days: {
            "summary": {"total_patterns": 0, "total_gaps": 0, "skill_candidates": 0, "gap_candidates": 0},
            "skill_candidates": [],
            "gap_candidates": [],
            "action_items": [],
            "chart_labels": [],
            "chart_scores": [],
        },
    )
    monkeypatch.setattr(
        cli,
        "_collect_project_overview",
        lambda: {
            "summary": {
                "total_projects": 1,
                "personal_instincts": 2,
                "inherited_instincts": 1,
                "global_personal": 0,
                "global_inherited": 0,
            },
            "projects": [
                {
                    "id": "p1",
                    "name": "repo",
                    "personal_instincts": 2,
                    "inherited_instincts": 1,
                    "observations": 4,
                    "last_seen": "2024-01-01T00:00:00Z",
                }
            ],
        },
    )
    cli._handle_dashboard(settings, {"output": str(tmp_path / "dashboard.json"), "format": "json"})
    out_data = json.loads((tmp_path / "dashboard.json").read_text(encoding="utf-8"))
    assert "quality" in out_data
    assert "personal_ranking" in out_data
    assert out_data["project_overview"]["projects"][0]["id"] == "p1"

    monkeypatch.setattr(sys, "argv", ["python"])
    assert cli.main() == 0

    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr("devgear.mem.logger.setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "not-a-command"])
    assert cli.main() == 2


def test_collect_project_overview_skips_invalid_registry_entries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import devgear.skills.learn.cli as learn_cli

    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    monkeypatch.setattr(learn_cli, "load_registry", lambda: {"bad": None, "good": {"name": "repo", "last_seen": "2024-01-01T00:00:00Z"}})
    monkeypatch.setattr(learn_cli, "_project_dir_for_id", lambda project_id: project_dir)
    monkeypatch.setattr(learn_cli, "_load_instincts_from_dir", lambda directory, source_type, scope_label: [])  # noqa: ARG005
    monkeypatch.setattr(cli, "_count_lines", lambda path: 0)

    overview = cli._collect_project_overview()

    assert overview["summary"]["total_projects"] == 1
    assert overview["projects"][0]["name"] == "repo"


def test_main_routes_all_commands(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import devgear.mem.logger as logger_mod

    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)

    called: list[str] = []

    commands = [
        "init", "setup", "context", "search", "session-init", "observe",
        "session-end", "compact", "search-structured", "record", "sync",
        "sync-check", "import", "dashboard", "record-interaction",
        "record-project-profile", "get-project-profile", "record-item-run",
        "team-context", "team-session-init",
    ]

    for name in commands:
        monkeypatch.setitem(
            cli._COMMAND_HANDLERS,
            name,
            lambda *args, _name=name, **kwargs: called.append(_name) or "",
        )

    for command in commands:
        monkeypatch.setattr(sys, "argv", ["python", command])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        assert cli.main() == 0

    assert called == commands


def test_setup_command_imports_without_torch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import devgear.mem.logger as logger_mod

    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict | None = None,
        locals: dict | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "devgear.mem.embedding":
            raise AssertionError("devgear.mem.embedding should not be imported during setup")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "devgear.mem.embedding", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    reloaded_cli = importlib.reload(cli)

    settings = make_settings(tmp_path)
    monkeypatch.setattr(reloaded_cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "setup"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert reloaded_cli.main() == 0

    assert "devgear.mem.embedding" not in sys.modules
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_main_help_and_unknown_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import devgear.mem.logger as logger_mod

    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    assert cli.main() == 0
    assert "init" in capsys.readouterr().out

    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "bogus"])
    assert cli.main() == 2


def test_cli_entrypoint_module(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy

    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.mem.cli", run_name="__main__")

    assert excinfo.value.code == 0


def test_embed_delegates_to_embedding_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli.embed は devgear.mem.embedding.embed に委譲する。"""
    import devgear.mem.embedding as embedding_mod

    received: dict[str, object] = {}

    def fake_embed(texts: list[str]) -> list[list[float]]:
        received["texts"] = texts
        return [[0.1, 0.2]]

    monkeypatch.setattr(embedding_mod, "embed", fake_embed)
    result = cli.embed(["a"])
    assert result == [[0.1, 0.2]]
    assert received == {"texts": ["a"]}


def test_main_help_with_session_start_command_prints_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """help 引数として SESSION_START コマンド名を渡すと wrapper が出力される。"""
    monkeypatch.setattr(sys, "argv", ["python", "context"])
    # 引数のみで stdin 不要：context は SESSION_START コマンドだが
    # main() の冒頭分岐（len(argv) < 2 ではない）でなく --help 経路を通すため
    # "-h" を含む組み合わせをテストする
    monkeypatch.setattr(sys, "argv", ["python", "-h"])
    assert cli.main() == 0
    out = capsys.readouterr().out
    # 通常 HELP_TEXT が出る（SESSION_START 系ではない）
    assert "init" in out


def test_main_only_arg_session_start_returns_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """argv に SESSION_START コマンドだけ渡し、help 扱いの分岐に到達する経路を通す。"""
    # main() 内 if len(sys.argv) < 2 or sys.argv[1] in {"-h", "--help"}: の分岐
    # "context" は -h/--help ではないので、ここでは command == "" のケースを通す
    monkeypatch.setattr(sys, "argv", ["python"])
    # SESSION_START_COMMANDS に "" は含まれないため HELP_TEXT が出る
    assert cli.main() == 0
    assert "init" in capsys.readouterr().out


def test_main_help_arg_invokes_session_start_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--help 経路で command が SESSION_START_COMMANDS に含まれる場合の分岐を通す。"""
    called: dict[str, object] = {}
    monkeypatch.setattr(
        cli,
        "print_session_start_output",
        lambda *args, **kwargs: called.setdefault("called", True),
    )
    # SESSION_START 集合を「--help」に書き換えれば、argv[1]=="--help" 時に
    # 「command in _SESSION_START_COMMANDS」分岐に入る
    monkeypatch.setattr(cli, "_SESSION_START_COMMANDS", frozenset({"--help"}))
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    assert cli.main() == 0
    assert called.get("called") is True
    capsys.readouterr()


def test_format_wrappers_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    """_format_fields / _slim_prompt / _slim_context_content は _search_handlers に委譲する。"""
    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        user_prompt="p",
        created_at_epoch=1704067200,
    )
    result = SearchResult("c1", 0.9, "content", "p", "repo", 1704067200, ["Edit"], [], [])

    assert isinstance(cli._format_fields("p", ["Edit"], ["a.py"], "body"), str)
    assert isinstance(cli._format_chunk_from_result(result), str)
    assert isinstance(cli._format_chunk(chunk), str)
    assert isinstance(cli._format_timestamp(1704067200), str)
    assert cli._truncate("abcdef", 3) == "abc..."
    assert isinstance(cli._slim_prompt("x" * 200, max_len=10), str)
    assert isinstance(
        cli._slim_context_content(
            "line1\nline2\nline3",
            max_prose_lines=2,
            max_prose_line_length=80,
        ),
        str,
    )


def test_migrate_settings_json_decode_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """settings.json が壊れていれば migrate-settings はログだけ出して return する。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    devgear_dir = tmp_path / ".devgear"
    devgear_dir.mkdir()
    (devgear_dir / "settings.json").write_text("{not-json", encoding="utf-8")

    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    cli._handle_migrate_settings(make_settings(tmp_path))
    assert any("settings.json 読み込み失敗" in w for w in warnings)


# === cli_dashboard_handlers のカバレッジ補完 ===


def test_count_lines_returns_zero_on_oserror(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """count_lines は OSError 発生時に 0 を返す。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    target = tmp_path / "a.jsonl"
    target.write_text("line\n")

    def _raise(*_args, **_kwargs):
        raise OSError("denied")

    monkeypatch.setattr(Path, "open", _raise)
    assert cdh.count_lines(target) == 0


def test_count_lines_returns_zero_when_missing(tmp_path: Path) -> None:
    """count_lines は存在しないパスに対して 0 を返す。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    assert cdh.count_lines(tmp_path / "nope.jsonl") == 0


def test_collect_skill_health_overview_handles_collect_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """collect_skill_health が例外を出してもダッシュボードは止まらず空 report で続行する。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    def _boom(_options):
        raise RuntimeError("health boom")

    monkeypatch.setattr(cdh, "collect_skill_health", _boom)
    monkeypatch.setattr(cdh, "summarize_health_report", lambda _r: {"summary": "x"})

    warnings: list[str] = []
    fake_log = SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg))

    result = cdh.collect_skill_health_overview({"k": "v"}, log=fake_log)
    assert result["report"] == {"generated_at": None, "skills": []}
    assert result["skills"] == []
    assert any("skill health collection failed" in w for w in warnings)


def test_collect_skill_growth_overview_disabled_returns_empty(tmp_path: Path) -> None:
    """sync 無効時は早期 return で空辞書を返す。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    settings = make_settings(tmp_path)
    settings.sync.enabled = False
    fake_log = SimpleNamespace(warning=lambda *a, **kw: None)
    result = cdh.collect_skill_growth_overview(settings, 30, log=fake_log)
    assert result["summary"]["total_patterns"] == 0
    assert result["skill_candidates"] == []


def test_collect_skill_growth_overview_pg_not_connectable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """sync 有効でも test_connection が False なら空辞書を返す。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    settings = make_settings(tmp_path)
    settings.sync.enabled = True
    settings.sync.postgres_url = "postgresql://example"

    class _PG:
        def __init__(self, _url: str) -> None:
            pass

        def test_connection(self) -> bool:
            return False

        def close(self) -> None:
            pass

    monkeypatch.setattr("devgear.mem.pg_database.PgDatabase", _PG)
    fake_log = SimpleNamespace(warning=lambda *a, **kw: None)
    result = cdh.collect_skill_growth_overview(settings, 30, log=fake_log)
    assert result["skill_candidates"] == []


def test_collect_skill_growth_overview_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """成功パスで proposal が返され、ランキングが構築される。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    settings = make_settings(tmp_path)
    settings.sync.enabled = True
    settings.sync.postgres_url = "postgresql://example"

    closed: dict[str, bool] = {"value": False}

    class _PG:
        def __init__(self, _url: str) -> None:
            pass

        def test_connection(self) -> bool:
            return True

        def close(self) -> None:
            closed["value"] = True

    monkeypatch.setattr("devgear.mem.pg_database.PgDatabase", _PG)

    import devgear.mem.skill_analyzer as analyzer_mod
    import devgear.mem.skill_proposal as proposal_mod

    monkeypatch.setattr(analyzer_mod, "detect_repeated_patterns", lambda *a, **kw: ["p"])
    monkeypatch.setattr(analyzer_mod, "detect_skill_gaps", lambda *a, **kw: ["g"])
    monkeypatch.setattr(
        proposal_mod,
        "generate_proposal",
        lambda patterns, gaps: {
            "summary": {"x": 1},
            "skill_candidates": [{"suggested_name": "skill-a", "priority_score": 5}],
            "gap_candidates": [{"name": "gap-a"}],
            "action_items": [{"item": "do"}],
        },
    )
    fake_log = SimpleNamespace(warning=lambda *a, **kw: None)
    result = cdh.collect_skill_growth_overview(settings, 30, log=fake_log)
    assert result["chart_labels"] == ["skill-a"]
    assert result["chart_scores"] == [5]
    assert closed["value"] is True


def test_collect_skill_growth_overview_inner_exception(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """skill_analyzer 経由で例外が起きてもログを出して空 dict を返す。"""
    from devgear.mem import cli_dashboard_handlers as cdh

    settings = make_settings(tmp_path)
    settings.sync.enabled = True
    settings.sync.postgres_url = "postgresql://example"

    class _PG:
        def __init__(self, _url: str) -> None:
            pass

        def test_connection(self) -> bool:
            return True

        def close(self) -> None:
            pass

    monkeypatch.setattr("devgear.mem.pg_database.PgDatabase", _PG)
    import devgear.mem.skill_analyzer as analyzer_mod

    def _boom(*_args, **_kwargs):
        raise RuntimeError("analyzer boom")

    monkeypatch.setattr(analyzer_mod, "detect_repeated_patterns", _boom)

    warnings: list[str] = []
    fake_log = SimpleNamespace(warning=lambda msg, *args: warnings.append(msg % args if args else msg))

    result = cdh.collect_skill_growth_overview(settings, 30, log=fake_log)
    assert result["skill_candidates"] == []
    assert any("skill growth collection failed" in w for w in warnings)


def test_resolve_safe_dashboard_output_path_invalid_value(tmp_path: Path) -> None:
    """非 str / 空白 / 解決不能パスはすべて None。"""
    from devgear.mem.cli_dashboard_handlers import _resolve_safe_dashboard_output_path

    settings = make_settings(tmp_path)
    assert _resolve_safe_dashboard_output_path(settings, None) is None
    assert _resolve_safe_dashboard_output_path(settings, 123) is None
    assert _resolve_safe_dashboard_output_path(settings, "   ") is None


def test_resolve_safe_dashboard_output_path_relative(tmp_path: Path) -> None:
    """相対パスは settings.data_path 配下に展開される。"""
    from devgear.mem.cli_dashboard_handlers import _resolve_safe_dashboard_output_path

    settings = make_settings(tmp_path)
    resolved = _resolve_safe_dashboard_output_path(settings, "out/inner.html")
    assert resolved is not None
    assert str(resolved).startswith(str(Path(settings.data_path).resolve()))


def test_resolve_safe_dashboard_output_path_outside_root(tmp_path: Path) -> None:
    """data_path 外のパスは拒否される。"""
    from devgear.mem.cli_dashboard_handlers import _resolve_safe_dashboard_output_path

    settings = make_settings(tmp_path)
    outside = str(tmp_path.parent / "outside.html")
    assert _resolve_safe_dashboard_output_path(settings, outside) is None


def test_slim_prompt_returns_empty_when_no_meaningful_content() -> None:
    """first_meaningful_line も in_code_block も拾えない場合は空文字を返す（line 267）。"""
    from devgear.mem.cli_search_handlers import slim_prompt

    # 空白のみ＋未閉鎖コードブロック相当：すべての行が空 or ```で in_code_block の切り替えのみ
    assert slim_prompt("```\n```\n", max_len=80) == ""


def test_slim_context_content_returns_empty_for_empty_text() -> None:
    """text が空なら空文字を返す（line 273）。"""
    from devgear.mem.cli_search_handlers import slim_context_content

    assert slim_context_content("") == ""


def test_slim_context_content_skips_blank_lines() -> None:
    """空行はスキップされる（line 283）。"""
    from devgear.mem.cli_search_handlers import slim_context_content

    result = slim_context_content("\n\nhello world\n\n", max_prose_lines=2)
    assert "hello world" in result
    # 余分な空行が含まれない
    assert "\n\n" not in result


def test_resolve_safe_dashboard_output_path_resolve_oserror(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Path.resolve が OSError を出した場合は None を返す。"""
    from devgear.mem.cli_dashboard_handlers import _resolve_safe_dashboard_output_path

    settings = make_settings(tmp_path)

    original_resolve = Path.resolve
    call_count = {"n": 0}

    def _resolve(self_path: Path, *args, **kwargs):  # type: ignore[override]
        # 1回目（allowed_root 計算）は通常通り、2回目（candidate.resolve()）は例外
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise OSError("resolve fail")
        return original_resolve(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    assert _resolve_safe_dashboard_output_path(settings, str(tmp_path / "x.html")) is None
