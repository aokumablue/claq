"""bluecore.mem.cli のエラーパステスト。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import bluecore.mem.cli as cli
import bluecore.mem.compaction as compaction_mod
from bluecore.mem.models import MemoryChunk
from bluecore.mem.row_converters import _parse_json_list
from bluecore.mem.search import SearchResult
from tests.mem.conftest import FakeDB, make_settings


def test_helper_functions_and_render_missing_chunk() -> None:
    """補助関数の None / 欠損チャンク分岐を通す。"""
    assert _parse_json_list(None) == []
    assert _parse_json_list("not-json") == []

    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    rendered = cli._render_adaptive_context(
        db,
        [
            SearchResult("missing", 0.9, "", "", "", 0, [], [], []),
            SearchResult("c1", 0.8, "", "", "", 0, [], [], []),
        ],
    )
    assert rendered.startswith("<mem-context>")
    assert "missing" not in rendered
    assert "content" in rendered

    old_chunk = MemoryChunk(
        id="old",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="old",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1704067100,
    )
    new_chunk = MemoryChunk(
        id="new",
        session_id="s1",
        project="repo",
        chunk_index=1,
        content="new",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1704067300,
    )
    filter_db = FakeDB([old_chunk, new_chunk])


def test_handler_exception_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """主要ハンドラの例外パスをまとめて通す。"""
    settings = make_settings(tmp_path, auto_compact_enabled=False)
    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))

    monkeypatch.setattr(cli, "_open_db", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    cli._handle_session_init(settings, {"cwd": str(tmp_path), "session_id": "s1", "prompt": "prompt"})
    cli._handle_observe(settings, {"cwd": str(tmp_path), "session_id": "s1", "tool_name": "Write"})
    cli._handle_session_end(settings, {"session_id": "s1"})
    cli._handle_compact(settings)
    cli._handle_record(settings, {"content": "note"})

    captured = capsys.readouterr()
    assert any("セッション初期化失敗" in warning for warning in warnings)
    assert any("チャンク保存失敗" in warning for warning in warnings)
    assert any("セッション終了失敗" in warning for warning in warnings)
    assert any("DB に接続できません" in captured.err for _ in [0])
    payloads = [json.loads(line) for line in captured.out.splitlines() if line.startswith("{")]
    assert any(payload.get("error") == "boom" for payload in payloads)


def test_session_end_inner_failures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """SessionEnd の内部 try/except を通す。"""
    settings = make_settings(tmp_path, auto_compact_enabled=False)
    chunk = MemoryChunk(
        id="c1",
        session_id="s1",
        project="repo",
        chunk_index=0,
        content="content",
        tool_names=["Edit"],
        files_read=[],
        files_modified=[],
        user_prompt="prompt",
        created_at_epoch=1704067200,
    )
    db = FakeDB([chunk])
    warnings: list[str] = []
    monkeypatch.setattr(cli.log, "warning", lambda msg, *args: warnings.append(msg % args if args else msg))
    monkeypatch.setattr(cli, "embed", lambda texts: [[0.1, 0.2]])
    monkeypatch.setattr(compaction_mod, "detect_low_quality", lambda db: [])
    monkeypatch.setattr(cli.time, "time", lambda: 100.0)

    def fake_execute(sql: str, params=None):  # noqa: ANN001
        if "optimize" in sql:
            raise RuntimeError("optimize boom")
        return SimpleNamespace(fetchone=lambda: (0,), fetchall=lambda: [])

    db.conn.execute = fake_execute  # type: ignore[method-assign]
    monkeypatch.setattr(cli, "_open_db", lambda settings: db)

    cli._handle_session_end(settings, {"session_id": "s1"})
    assert any("FTS5 最適化失敗" in warning for warning in warnings)


def test_record_and_profile_handlers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """記録系ハンドラの成功系と早期 return を通す。"""
    settings = make_settings(tmp_path, auto_compact_enabled=False)
    db = FakeDB()

    monkeypatch.setattr(cli, "_open_db", lambda settings: db)
    monkeypatch.setattr(cli, "get_git_user_name", lambda: "origin")

    cli._handle_record_interaction(settings, {"session_id": "s1", "user_prompt_full": ""})
    result = json.loads(capsys.readouterr().out)
    assert result["success"] is True
    assert result["skipped"] is True
    assert result["reason"] == "no prompt"

    # Claude Code UserPromptSubmit は "prompt" キーで渡す: フォールバック確認
    cli._handle_record_interaction(
        settings,
        {"session_id": "s1", "prompt": "prompt via new key"},
    )
    result2 = json.loads(capsys.readouterr().out)
    assert result2["success"] is True
    assert "skipped" not in result2

    cli._handle_record_interaction(
        settings,
        {
            "session_id": "s1",
            "user_prompt_full": "prompt",
            "ai_response_summary": "summary",
            "ai_response_tool_plan": "plan",
            "chunk_id": "c1",
            "execution_outcome": "success",
            "tool_error_count": 2,
        },
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["success"] is True
    # "prompt" キー経由で先に1件追加されているため index は 1
    assert payload["interaction_index"] == 1
    assert db.interactions[1].ai_response_summary == "summary"

    assert cli._handle_record_project_profile(
        settings,
        {
            "project": "repo",
            "project_path": "/repo",
            "languages": ["python"],
            "frameworks": ["pytest"],
            "primary_language": "python",
            "test_command": "pytest",
            "build_command": "build",
            "scope_hint": "project",
        },
    ) == ""
    assert capsys.readouterr().out == ""
    assert db.project_profiles["repo"].languages == ["python"]

    cli._handle_get_project_profile(settings, {"project": "repo"})
    payload = json.loads(capsys.readouterr().out)
    assert payload["found"] is True
    assert payload["project"] == "repo"

    cli._handle_get_project_profile(settings, {"project": "missing"})
    assert json.loads(capsys.readouterr().out) == {"found": False}


def test_record_and_profile_failure_paths(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """記録系ハンドラの例外パスを通す。"""
    settings = make_settings(tmp_path, auto_compact_enabled=False)
    monkeypatch.setattr(cli, "_open_db", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(cli, "get_git_user_name", lambda: "origin")

    cli._handle_record_interaction(
        settings,
        {"session_id": "s1", "user_prompt_full": "prompt"},
    )
    assert cli._handle_record_project_profile(settings, {"project": "repo"}) == ""
    assert cli._handle_get_project_profile(settings, {"project": "repo"}) is None

    payloads = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line]
    assert any(payload.get("error") == "boom" for payload in payloads)
    assert any(payload.get("success") is False for payload in payloads)


