"""bluecore.mem.cli の追加テスト。"""

from __future__ import annotations

import builtins
import importlib
import io
import json
import sys
from pathlib import Path

import pytest

import bluecore.mem.importers as importers_mod
from bluecore.mem import cli
from bluecore.mem.database import MemoryChunk
from bluecore.mem.search import SearchResult
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
        created_at_epoch=1704067300,
    )
    db = FakeDB([chunk_a, chunk_b])


    rendered = cli._render_adaptive_context(
        db,
        [
            SearchResult("c1", 0.9, "", "", 0, [], [], []),
            SearchResult("c2", 0.8, "", "", 0, [], [], []),
        ],
    )
    assert rendered.startswith("<mem-context>")
    assert "## repo (2024-01-01 00:00)" in rendered
    assert "..." in rendered
    assert cli._format_chunk(chunk_a).startswith("**ツール**")
    rich_result = SearchResult(
        "team-1",
        0.9,
        "z" * 600,
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
    # 予算 1 トークンではエントリが 1 件も入らず、ヘッダのみが残る
    assert "zzzz" not in tiny_render
    assert "**ツール**" not in tiny_render
    assert cli._format_timestamp(1704067200) == "2024-01-01 00:00"
    assert cli._truncate("abc", 10) == "abc"


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
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    import bluecore.mem.compaction as compaction_mod

    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(compaction_mod, "detect_low_quality", lambda db: ["c1"])
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)

    cli._handle_session_end(settings, {"session_id": "s1"})
    assert db.embeddings == [(["c1"], [[0.1, 0.2]])]
    assert settings.last_compacted_at == 100.0

    monkeypatch.setattr(sys, "argv", ["python", "--execute"])
    cli._handle_compact(settings)
    assert any("DELETE FROM memory_chunks" in sql for sql, _ in db.executed)


def _make_chunk(chunk_id: str | None, index: int) -> MemoryChunk:
    """reembed テスト用のチャンクを作成する。"""
    return MemoryChunk(
        id=chunk_id,
        session_id="s1",
        project="repo",
        chunk_index=index,
        content=f"content-{index}",
        tool_names=[],
        files_read=[],
        files_modified=[],
        created_at_epoch=1704067200,
    )


def test_handle_reembed_regenerates_all_embeddings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """reembed は vec テーブルを再作成し、id を持つ全チャンクを再埋め込みする。"""
    chunks = [_make_chunk("c0", 0), _make_chunk(None, 1), _make_chunk("c2", 2)]
    db = FakeDB(chunks)
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2] for _ in texts])

    cli._handle_reembed(settings)

    assert db.vec_recreated is True
    assert db.embeddings == [(["c0", "c2"], [[0.1, 0.2], [0.1, 0.2]])]
    assert "2 件" in capsys.readouterr().out


def test_handle_reembed_processes_in_batches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """チャンク数がバッチサイズを超える場合は分割して埋め込む。"""
    chunks = [_make_chunk(f"c{i}", i) for i in range(300)]
    db = FakeDB(chunks)
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1] for _ in texts])

    cli._handle_reembed(settings)

    assert len(db.embeddings) == 2
    assert len(db.embeddings[0][0]) == 256
    assert len(db.embeddings[1][0]) == 44


def test_handle_reembed_skips_without_sqlite_vec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """sqlite-vec が利用できない場合はメッセージを出してスキップする。"""
    db = FakeDB([_make_chunk("c0", 0)])
    db.vec_available = False
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1] for _ in texts])

    cli._handle_reembed(settings)

    assert db.embeddings == []
    assert "スキップ" in capsys.readouterr().out


def test_handle_reembed_aborts_when_model_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """埋め込みモデル未配置（embed が空を返す）の場合は中断する。"""
    db = FakeDB([_make_chunk("c0", 0)])
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [])

    cli._handle_reembed(settings)

    assert db.embeddings == []
    assert "中断" in capsys.readouterr().err


def test_handle_setup_and_observe_branches(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))

    assert cli._handle_setup(settings) == ""
    assert settings.data_path.exists()

    import bluecore.mem.chunker as chunker_mod

    monkeypatch.setattr(chunker_mod, "build_chunk_from_tool_use", lambda session_id, project, chunk_index, params: MemoryChunk(
        session_id=session_id,
        project=project,
        chunk_index=chunk_index,
        content="observed",
        tool_names=[params.tool_name],
        files_read=[],
        files_modified=[],
        created_at_epoch=1700000000,
    ))
    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "Write",
            "tool_input": {"path": "file.py"},
            "tool_response": "ok",
            "prompt": "read file",
        },
    )
    assert db.stored_chunks


def test_handle_observe_skips_non_observed_tool(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PostToolUse の matcher が "*" に広がっても、Read 等の対象外ツールはチャンク保存しない（早期 return）。"""
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))
    assert cli._handle_setup(settings) == ""

    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "Read",
            "tool_input": {"file_path": "file.py"},
            "tool_response": "ok",
            "prompt": "read file",
        },
    )

    assert db.stored_chunks == []


def test_handle_observe_skips_copilot_lowercase_non_observed_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copilot CLI の lowercase 対象外ツール名（read）でも早期 return する。"""
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))
    assert cli._handle_setup(settings) == ""

    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "read",
            "tool_input": {"file_path": "file.py"},
            "tool_response": "ok",
            "prompt": "read file",
        },
    )

    assert db.stored_chunks == []


def test_handle_observe_records_copilot_lowercase_observed_tool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Copilot CLI の lowercase 記録対象ツール名（write）ではチャンクを保存する。"""
    settings = make_settings(tmp_path)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))
    assert cli._handle_setup(settings) == ""

    import bluecore.mem.chunker as chunker_mod

    monkeypatch.setattr(
        chunker_mod,
        "build_chunk_from_tool_use",
        lambda session_id, project, chunk_index, params: MemoryChunk(
            session_id=session_id,
            project=project,
            chunk_index=chunk_index,
            content="observed",
            tool_names=[params.tool_name],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        ),
    )
    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "write",
            "tool_input": {"file_path": "file.py", "content": "x"},
            "tool_response": "ok",
            "prompt": "write file",
        },
    )

    assert db.stored_chunks


def test_import_helpers(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)

    import_calls: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB()))
    monkeypatch.setattr(importers_mod, "import_instincts", lambda db, origin_user: import_calls.append(("instincts", origin_user, None)) or 1)
    monkeypatch.setattr(importers_mod, "import_adrs", lambda db, origin_user, repo_root: import_calls.append(("adrs", origin_user, repo_root)) or 2)
    monkeypatch.setattr(importers_mod, "import_event_logs", lambda db, origin_user: import_calls.append(("events", origin_user, None)) or 3)
    cli._handle_import(settings, {"types": ["instincts", "adrs", "events"], "repo_root": "/repo"})
    assert json.loads(capsys.readouterr().out)["imported"] == {"instincts": 1, "adrs": 2, "events": 3}


def test_handle_session_end_empty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = make_settings(tmp_path)
    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(FakeDB([])))
    cli._handle_session_end(settings, {"session_id": "s1"})
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
    import bluecore.mem.logger as logger_mod

    settings = make_settings(tmp_path)

    monkeypatch.setattr(cli.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "context"])
    assert cli.main() == 0
    captured = capsys.readouterr()
    assert captured.err == ""
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
    import bluecore.mem.logger as logger_mod

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


def test_parse_argv_and_stdin_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    """_parse_argv_and_stdin の stdin 各分岐（tty/空/非dict/正常）を網羅する。"""

    class _FakeStdin:
        def __init__(self, *, tty: bool, data: str) -> None:
            self._tty = tty
            self._data = data

        def isatty(self) -> bool:
            return self._tty

        def read(self) -> str:
            return self._data

    monkeypatch.setattr(sys, "argv", ["prog", "search"])

    # stdin が tty → 読み取らず空 dict
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True, data="{}"))
    assert cli._parse_argv_and_stdin() == ("search", {})

    # 空入力 → strip で偽となり空 dict
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False, data="   "))
    assert cli._parse_argv_and_stdin() == ("search", {})

    # JSON が dict 以外（リスト）→ 無視して空 dict
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False, data="[1, 2]"))
    assert cli._parse_argv_and_stdin() == ("search", {})

    # 正常系: dict をパース
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=False, data='{"query": "x"}'))
    assert cli._parse_argv_and_stdin() == ("search", {"query": "x"})

    # argv に command 無し → command は空文字
    monkeypatch.setattr(sys, "argv", ["prog"])
    monkeypatch.setattr(sys, "stdin", _FakeStdin(tty=True, data=""))
    assert cli._parse_argv_and_stdin() == ("", {})


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
    import bluecore.mem.logger as logger_mod

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
    import bluecore.mem.logger as logger_mod

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
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    import bluecore.mem.compaction as compaction_mod

    monkeypatch.setattr(cli, "_open_db", lambda settings: open_fake_db(db))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(compaction_mod, "detect_low_quality", lambda db: (_ for _ in ()).throw(RuntimeError("compact boom")))
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)
    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    cli._handle_session_end(settings, {"session_id": "s1"})
    assert any("自動圧縮エラー" in message for message in warnings)


def test_setup_command_imports_without_torch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import bluecore.mem.logger as logger_mod

    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict | None = None,
        locals: dict | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "bluecore.mem.embedding":
            raise AssertionError("bluecore.mem.embedding should not be imported during setup")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.delitem(sys.modules, "bluecore.mem.embedding", raising=False)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    reloaded_cli = importlib.reload(cli)

    settings = make_settings(tmp_path)
    monkeypatch.setattr(reloaded_cli.Settings, "load", lambda: settings)
    monkeypatch.setattr(logger_mod, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(sys, "argv", ["python", "setup"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

    assert reloaded_cli.main() == 0

    assert "bluecore.mem.embedding" not in sys.modules
    payload = json.loads(capsys.readouterr().out)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_main_help_and_unknown_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import bluecore.mem.logger as logger_mod

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
        runpy.run_module("bluecore.mem.cli", run_name="__main__")

    assert excinfo.value.code == 0


def test_embed_delegates_to_embedding_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """cli.embed は bluecore.mem.embedding.embed に委譲する。"""
    import bluecore.mem.embedding as embedding_mod

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
    """_format_fields / _slim_context_content は _search_handlers に委譲する。"""
    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        created_at_epoch=1704067200,
    )
    result = SearchResult("c1", 0.9, "content", "repo", 1704067200, ["Edit"], [], [])

    assert isinstance(cli._format_fields(["Edit"], ["a.py"], "body"), str)
    assert isinstance(cli._format_chunk_from_result(result), str)
    assert isinstance(cli._format_chunk(chunk), str)
    assert isinstance(cli._format_timestamp(1704067200), str)
    assert cli._truncate("abcdef", 3) == "abc..."
    assert isinstance(
        cli._slim_context_content(
            "line1\nline2\nline3",
            max_prose_lines=2,
            max_prose_line_length=80,
        ),
        str,
    )


def test_slim_context_content_returns_empty_for_empty_text() -> None:
    """text が空なら空文字を返す（line 273）。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    assert slim_context_content("") == ""


def test_slim_context_content_drops_lines_that_compact_to_empty() -> None:
    """strip 後は非空でも compact_line が空を返す行（見出し記号のみ等）は落とす。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    result = slim_context_content("###\nreal prose line\n")

    assert "real prose line" in result
    assert "###" not in result


def test_slim_context_content_skips_blank_lines() -> None:
    """空行はスキップされる（line 283）。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    result = slim_context_content("\n\nhello world\n\n", max_prose_lines=2)
    assert "hello world" in result
    # 余分な空行が含まれない
    assert "\n\n" not in result


def test_slim_context_content_prose_over_limit() -> None:
    """prose 行数上限超で省略記号を一度だけ付け以降はスキップする。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    out = slim_context_content("a\nb\nc", max_prose_lines=1)
    assert out.count("...") == 1


def test_record_deps_requires_get_git_user_name() -> None:
    """RecordDeps は get_git_user_name を必須依存として要求する。"""
    from bluecore.mem.cli_record_handlers import RecordDeps

    with pytest.raises(TypeError):
        RecordDeps(open_db=lambda settings: None, get_project=lambda data: "p", log=lambda *args: None)


def test_format_fields_limits_files_modified_to_two() -> None:
    """変更ファイルは先頭 2 件のみ表示する（注入トークン削減）。"""
    from bluecore.mem.cli_search_handlers import format_fields

    out = format_fields(["Edit"], ["a.py", "b.py", "c.py", "d.py"], "")
    assert "**変更ファイル**: a.py, b.py" in out
    assert "c.py" not in out


def test_slim_context_content_caps_code_block_lines() -> None:
    """コードブロックは max_code_lines 行で打ち切り省略記号を付ける。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    code = "\n".join(f"line{i}" for i in range(30))
    out = slim_context_content(f"```python\n{code}\n```", max_code_lines=20)

    assert "line19" in out
    assert "line20" not in out
    assert out.count("...") == 1
    assert out.count("```") == 2


def test_slim_context_content_keeps_short_code_block_intact() -> None:
    """上限以下のコードブロックは全行そのまま通す。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    out = slim_context_content("```\na\nb\n```", max_code_lines=20)
    assert "a" in out and "b" in out
    assert "..." not in out


def test_slim_context_content_closes_unclosed_fence() -> None:
    """閉じフェンスなしで終端する入力にはフェンスを補完する。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    out = slim_context_content("```python\nline1\nline2", max_code_lines=1)
    assert out.endswith("```")
    assert out.count("```") == 2


def test_slim_context_content_clips_long_code_lines() -> None:
    """コードブロック内の行も max_prose_line_length でクリップされる。"""
    from bluecore.mem.cli_search_handlers import slim_context_content

    long_line = "x" * 500
    out = slim_context_content(f"```\n{long_line}\n```", max_prose_line_length=160)

    code_line = out.splitlines()[1]
    assert len(code_line) == 160


def test_handle_observe_normalizes_apply_patch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Codex の apply_patch ツール名は Edit に正規化して記録する。"""
    settings = make_settings(tmp_path)
    db = FakeDB()
    monkeypatch.setattr(cli, "_open_db", lambda current_settings: open_fake_db(db))
    assert cli._handle_setup(settings) == ""

    import bluecore.mem.chunker as chunker_mod

    monkeypatch.setattr(
        chunker_mod,
        "build_chunk_from_tool_use",
        lambda session_id, project, chunk_index, params: MemoryChunk(
            session_id=session_id,
            project=project,
            chunk_index=chunk_index,
            content="observed",
            tool_names=[params.tool_name],
            files_read=[],
            files_modified=[],
            created_at_epoch=1700000000,
        ),
    )
    cli._handle_observe(
        settings,
        {
            "session_id": "s1",
            "cwd": str(tmp_path),
            "tool_name": "apply_patch",
            "tool_input": {"input": "*** Begin Patch\n*** Update File: a.py\n*** End Patch"},
            "tool_response": "ok",
            "prompt": "patch file",
        },
    )
    assert db.stored_chunks
    assert db.stored_chunks[0].tool_names == ["Edit"]
