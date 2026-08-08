"""フックヘルパー関数と挙動のテスト。

ドキュメントファイル警告、設定保護、セッションライフサイクル、
およびコンパクト提案ロジックを対象とする。
"""

from __future__ import annotations

import io
import json
import os
import runpy
import sys
import time
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace

import pytest

from bluecore.hooks import (
    config_protection as config_protection,
)
from bluecore.hooks import (
    session_end as session_end,
)
from bluecore.hooks import (
    session_start as session_start,
)
from bluecore.hooks.hook_common import is_truthy


def _patch_stdin_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """hook_common.read_raw_stdin* が使う select を常に ready 扱いにする。

    io.StringIO は実 fd を持たないため、select.select をそのまま通すと
    io.UnsupportedOperation で落ちる（_stdin_ready の TTY/タイムアウト
    ガードは launcher._read_stdin から移設済み）。
    """
    from bluecore.hooks import hook_common

    monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: (r, [], []))


def test_config_protection_blocks_protected_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdin_ready(monkeypatch)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"tool_name": "Write", "tool_input": {"file_path": "eslint.config.js"}})),
    )

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Modifying eslint.config.js is not allowed" in stderr.getvalue()
    assert stdout.getvalue() == ""


def test_config_protection_blocks_model_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """ダウンロード完全性の信頼アンカーである model.json の書き換えをブロックする。"""
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "plugins/bluecore/model.json"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Modifying model.json is not allowed" in stderr.getvalue()


def test_config_protection_allows_safe_file(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "README.md"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 0

    # 許可時は stdout は空（パススルー不要）
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_config_protection_blocks_protected_file_in_apply_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex の apply_patch パッチ内の保護ファイルをブロックする。"""
    _patch_stdin_ready(monkeypatch)
    patch = "*** Begin Patch\n*** Update File: ruff.toml\n@@\n-a\n+b\n*** End Patch"
    payload = json.dumps({"tool_name": "apply_patch", "tool_input": {"input": patch}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Modifying ruff.toml is not allowed" in stderr.getvalue()


def test_config_protection_blocks_unparseable_apply_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """パース不能な apply_patch 入力は fail-closed でブロックする。"""
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_name": "apply_patch", "tool_input": {"input": "garbage"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Could not determine target files" in stderr.getvalue()


def test_config_protection_allows_safe_apply_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象を含まない apply_patch は許可する。"""
    _patch_stdin_ready(monkeypatch)
    patch = "*** Begin Patch\n*** Update File: src/main.py\n@@\n-a\n+b\n*** End Patch"
    payload = json.dumps({"tool_name": "apply_patch", "tool_input": {"input": patch}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 0

    assert stderr.getvalue() == ""


def test_config_protection_blocks_legacy_file_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """file フィールドのみ持つ入力でも保護ファイルをブロックする。"""
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_name": "Write", "tool_input": {"file": "biome.json"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Modifying biome.json is not allowed" in stderr.getvalue()


def test_session_start_deduplicates_recent_sessions(tmp_path: Path) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()

    older = first_dir / "daily-session.tmp"
    newer = second_dir / "daily-session.tmp"
    older.write_text("older", encoding="utf-8")
    newer.write_text("newer", encoding="utf-8")
    now = time.time()
    os.utime(older, (now - 120, now - 120))
    os.utime(newer, (now - 60, now - 60))

    result = session_start.dedupe_recent_sessions([first_dir, second_dir])

    assert [item["path"] for item in result] == [str(newer)]
    assert result[0]["basename"] == "daily-session.tmp"


def test_session_end_extracts_summary(tmp_path: Path) -> None:
    transcript = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "user", "content": "Fix docs"},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "Edit", "input": {"file_path": "README.md"}},
                    {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}},
                ]
            },
        },
        {"type": "tool_use", "tool_name": "Write", "tool_input": {"file_path": "docs/notes.md"}},
        {"type": "user", "message": {"content": [{"text": "Add tests"}]}},
    ]
    transcript.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")

    summary = session_end.extract_session_summary(str(transcript))

    assert summary is not None
    assert summary["userMessages"] == ["Fix docs", "Add tests"]
    assert summary["toolsUsed"] == ["Bash", "Edit", "Write"]
    assert summary["filesModified"] == ["README.md", "docs/notes.md"]
    assert summary["totalMessages"] == 2


def test_config_protection_entrypoint_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_input": {"file_path": "README.md"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "argv", ["config_protection.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.config_protection", run_name="__main__")

    assert excinfo.value.code == 0


def test_session_end_run_logs_outer_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(session_end, "get_sessions_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(session_end, "log", logs.append)

    assert session_end.run("{}") == "{}"
    assert any("Error: boom" in message for message in logs)


def test_session_start_main_sanitizes_exception_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(session_start, "read_raw_stdin", lambda: "raw")
    monkeypatch.setattr(session_start, "run", lambda raw: (_ for _ in ()).throw(RuntimeError("boom\nbad\x1b[31m")))
    monkeypatch.setattr(session_start, "log", logs.append)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert session_start.main() == 0

    assert stdout.getvalue().strip().startswith("{")
    assert any("[SessionStart] Error" in message for message in logs)
    assert any("[SessionStart] Error" in message and "\n" not in message and "\x1b" not in message for message in logs)


def test_hook_common_is_truthy_handles_falsey_values() -> None:
    assert is_truthy(None) is False
    assert is_truthy("") is False
    assert is_truthy("0") is False
    assert is_truthy(" no ") is False


def test_session_start_main_success_and_entrypoint(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(session_start, "read_raw_stdin", lambda: "raw")
    monkeypatch.setattr(session_start, "run", lambda raw: raw + "-out")

    assert session_start.main() == 0
    assert capsys.readouterr().out == "raw-out"

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "argv", ["session_start.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.session_start", run_name="__main__")

    assert excinfo.value.code == 0


class TestImportAdrsAndInstincts:
    def test_success_direct(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """_import_adrs_and_instincts の正常系: DB 呼び出しと log を確認。"""
        logs: list[str] = []
        fake_db = SimpleNamespace(close=lambda: None)

        import bluecore.hooks.session_start as ss_mod
        import bluecore.mem.importers as importers_mod
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(ss_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "bluecore.hooks.session_start.get_git_user_name", lambda: "user", raising=False
        )
        monkeypatch.setattr(importers_mod, "import_instincts", lambda db, user: 3)
        monkeypatch.setattr(importers_mod, "import_adrs", lambda db, user, repo_root: 2)

        import bluecore.mem.database as db_mod

        monkeypatch.setattr(db_mod, "Database", lambda path: fake_db)

        ss_mod._import_adrs_and_instincts()

        assert any("instincts=3" in msg and "adrs=2" in msg for msg in logs)

    def test_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_import_adrs_and_instincts の異常系: 例外が log に記録される。"""
        logs: list[str] = []

        import bluecore.hooks.session_start as ss_mod
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(ss_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("db-fail"))
        )

        ss_mod._import_adrs_and_instincts()

        assert any("mem import error" in msg for msg in logs)

class TestRecordStopEvent:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """_record_stop_event の正常系: store_event_log が呼ばれる。"""
        logs: list[str] = []
        stored: list[object] = []
        fake_db = SimpleNamespace(store_event_log=stored.append, close=lambda: None)

        import bluecore.hooks.session_end as se_mod
        import bluecore.mem.database as db_mod
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "bluecore.hooks.session_end.get_git_user_name", lambda: "user", raising=False
        )
        monkeypatch.setattr(db_mod, "Database", lambda path: fake_db)

        summary = {"toolsUsed": ["Bash"], "filesModified": ["a.py"], "totalMessages": 5}
        se_mod._record_stop_event(summary, {"project": "myproj", "branch": "main"})

        assert len(stored) == 1
        event = stored[0]
        assert event.event_type == "session_stop"
        assert event.project_id == "myproj"
        content = json.loads(event.content)
        assert content["tools_used"] == ["Bash"]
        assert content["total_messages"] == 5
        assert any("event_log recorded" in msg for msg in logs)

    def test_no_summary(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """summary=None のとき tools_used などが空リストになる。"""
        stored: list[object] = []
        fake_db = SimpleNamespace(store_event_log=stored.append, close=lambda: None)

        import bluecore.hooks.session_end as se_mod
        import bluecore.mem.database as db_mod
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", lambda msg: None)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "bluecore.hooks.session_end.get_git_user_name", lambda: "user", raising=False
        )
        monkeypatch.setattr(db_mod, "Database", lambda path: fake_db)

        se_mod._record_stop_event(None, {"project": "proj", "branch": "main"})

        content = json.loads(stored[0].content)
        assert content["tools_used"] == []
        assert content["files_modified"] == []
        assert content["total_messages"] == 0

    def test_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_record_stop_event の異常系: 例外が log に記録される。"""
        logs: list[str] = []

        import bluecore.hooks.session_end as se_mod
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        )

        se_mod._record_stop_event(None, {"project": "p"})

        assert any("event_log error" in msg for msg in logs)

    def test_session_end_run_calls_record_event(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """session_end.run() が _record_stop_event を呼ぶことを確認。"""
        called: list[tuple] = []

        import bluecore.hooks.session_end as se_mod

        monkeypatch.setattr(se_mod, "get_sessions_dir", lambda: tmp_path / "sessions")
        monkeypatch.setattr(se_mod, "get_date_string", lambda: "2024-01-01")
        monkeypatch.setattr(se_mod, "get_session_id_short", lambda: "abc")
        monkeypatch.setattr(se_mod, "get_session_metadata", lambda: {"project": "p", "branch": "main", "worktree": str(tmp_path)})
        monkeypatch.setattr(se_mod, "ensure_dir", lambda path: None)
        monkeypatch.setattr(se_mod, "get_time_string", lambda: "12:00")
        monkeypatch.setattr(se_mod, "extract_session_summary", lambda path: None)
        monkeypatch.setattr(se_mod, "write_file", lambda path, content: None)
        monkeypatch.setattr(se_mod, "read_file", lambda path: None)
        monkeypatch.setattr(se_mod, "log", lambda msg: None)
        monkeypatch.setattr(se_mod, "_record_stop_event", lambda summary, metadata: called.append((summary, metadata)))

        se_mod.run("{}")

        assert len(called) == 1
        assert called[0][1]["project"] == "p"
