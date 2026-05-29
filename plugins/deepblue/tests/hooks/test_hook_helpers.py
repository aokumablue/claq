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

from devgear.hooks import (
    config_protection as config_protection,
)
from devgear.hooks import (
    doc_file_warning as doc_file_warning,
)
from devgear.hooks import (
    session_end as session_end,
)
from devgear.hooks import (
    session_start as session_start,
)
from devgear.hooks import (
    suggest_compact as suggest_compact,
)
from devgear.hooks.hook_common import is_truthy


def test_doc_file_warning_flags_ad_hoc_documents() -> None:
    assert doc_file_warning.is_suspicious_doc_path("notes/TODO.md")
    assert doc_file_warning.is_suspicious_doc_path("scratch/WIP.txt")
    assert not doc_file_warning.is_suspicious_doc_path("docs/TODO.md")
    assert not doc_file_warning.is_suspicious_doc_path("commands/c-plan.md")


def test_config_protection_blocks_protected_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_input": {"file_path": "eslint.config.js"}})))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 2

    assert "Modifying eslint.config.js is not allowed" in stderr.getvalue()
    assert stdout.getvalue() == ""


def test_config_protection_allows_safe_file(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"tool_input": {"file_path": "README.md"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        assert config_protection.main() == 0

    # 許可時は stdout は空（パススルー不要）
    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_doc_file_warning_main_warns_for_ad_hoc_documents(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"tool_input": {"file_path": "notes/TODO.md"}})
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))

    with redirect_stdout(stdout), redirect_stderr(stderr):
        assert doc_file_warning.main() == 0

    # 警告のみで stdout は空（パススルー不要）
    assert stdout.getvalue() == ""
    assert "Ad-hoc documentation filename detected" in stderr.getvalue()


def test_suggest_compact_increments_counter_and_warns(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-123")
    monkeypatch.setenv("COMPACT_THRESHOLD", "2")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert suggest_compact.main() == 0
        assert suggest_compact.main() == 0

    output = stderr.getvalue()
    assert "2 tool calls reached" in output
    assert (tmp_path / "claude-tool-count-session-123").read_text(encoding="utf-8") == "2"


def test_suggest_compact_helpers_and_error_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert suggest_compact.sanitize_session_id(None) == "default"
    assert suggest_compact.sanitize_session_id("sess/ion!123") == "session123"
    assert suggest_compact.parse_threshold("10") == 10
    assert suggest_compact.parse_threshold("0") == 50
    assert suggest_compact.parse_threshold("bad") == 50

    monkeypatch.setattr(
        suggest_compact.Path,
        "open",
        lambda self, *args, **kwargs: (_ for _ in ()).throw(OSError("boom")),
    )
    writes: list[tuple[Path, str]] = []
    monkeypatch.setattr(suggest_compact, "write_file", lambda path, content: writes.append((Path(path), content)))

    counter = tmp_path / "counter"
    assert suggest_compact.read_and_increment(counter) == 1
    assert writes == [(counter, "1")]


def test_suggest_compact_main_logs_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(suggest_compact, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    stderr = io.StringIO()

    with redirect_stderr(stderr):
        assert suggest_compact.main() == 0

    assert "StrategicCompact" in stderr.getvalue()
    assert "boom" in stderr.getvalue()


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


def test_doc_file_warning_non_document_and_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"tool_input": {"file_path": "notes/README.png"}})

    assert not doc_file_warning.is_suspicious_doc_path("notes/README.png")

    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "argv", ["doc_file_warning.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.hooks.doc_file_warning", run_name="__main__")

    assert excinfo.value.code == 0


def test_config_protection_entrypoint_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = json.dumps({"tool_input": {"file_path": "README.md"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "argv", ["config_protection.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.hooks.config_protection", run_name="__main__")

    assert excinfo.value.code == 0


def test_session_end_run_logs_outer_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    logs: list[str] = []
    monkeypatch.setattr(session_end, "get_sessions_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(session_end, "log", logs.append)

    assert session_end.run("{}") == "{}"
    assert any("Error: boom" in message for message in logs)


def test_session_start_slim_injection_and_error_branch(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    learned_dir = tmp_path / "learned"
    sessions_dir = tmp_path / "sessions"
    learned_dir.mkdir()
    sessions_dir.mkdir()
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("slim-content", encoding="utf-8")
    logs: list[str] = []

    monkeypatch.setattr(session_start, "ensure_dir", lambda path: None)
    monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: learned_dir)
    monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
    monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [])
    monkeypatch.setattr(session_start, "find_files", lambda *args, **kwargs: [])
    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source="auto"))
    monkeypatch.setattr(
        session_start,
        "detect_project",
        lambda cwd: SimpleNamespace(languages=[], frameworks=[], primary_language=None),
    )
    monkeypatch.setattr(
        session_start.Settings,
        "load",
        lambda: SimpleNamespace(slim=SimpleNamespace(enabled=True)),
    )
    monkeypatch.setattr(session_start, "_SLIM_SKILL_PATH", skill_file)
    monkeypatch.setattr(session_start, "log", logs.append)

    payload = json.loads(session_start.run(json.dumps({"session_id": "abc"})))
    assert "slim-content" in payload["hookSpecificOutput"]["additionalContext"]

    monkeypatch.setattr(
        session_start,
        "_SLIM_SKILL_PATH",
        SimpleNamespace(
            exists=lambda: True,
            read_text=lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom\nbad\x1b[31m")),
        ),
    )
    assert json.loads(session_start.run(json.dumps({"session_id": "abc"})))["hookSpecificOutput"]["additionalContext"] == ""
    assert any("Slim injection error" in message for message in logs)
    assert any(
        "Slim injection error" in message and "\n" not in message and "\x1b" not in message
        for message in logs
    )


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


def test_session_start_sanitizes_git_logs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    logs: list[str] = []

    class FakeDatabase:
        def __init__(self, path: Path) -> None:
            self.path = path

        def upsert_project_profile(self, profile) -> None:  # noqa: ANN001
            self.profile = profile

        def close(self) -> None:
            pass

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(session_start, "log", logs.append)
    monkeypatch.setattr(session_start, "get_git_user_name", lambda: "me")
    monkeypatch.setattr(
        session_start,
        "check_output_text",
        lambda cmd, timeout=5.0: {
            ("git", "rev-parse", "--is-inside-work-tree"): "true",
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "feature\nbranch\x1b[31m",
            ("git", "rev-parse", "--short=12", "HEAD"): "abc123\x00def",
            ("git", "status", "--porcelain"): " M file.py\n",
        }[tuple(cmd)],
    )
    monkeypatch.setattr("devgear.mem.database.Database", FakeDatabase)
    monkeypatch.setattr(
        session_start.Settings,
        "load",
        lambda: SimpleNamespace(db_path=tmp_path / "mem.db", slim=SimpleNamespace(enabled=False)),
    )

    session_start._save_project_profile(
        SimpleNamespace(languages=["python"], frameworks=["pytest"], primary_language="python")
    )

    assert any("git branch=" in message for message in logs)
    assert all("\n" not in message and "\x1b" not in message for message in logs)


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
        runpy.run_module("devgear.hooks.session_start", run_name="__main__")

    assert excinfo.value.code == 0


def test_suggest_compact_invalid_counter_and_checkpoint_entrypoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invalid_counter = tmp_path / "invalid-counter"
    invalid_counter.write_text("oops", encoding="utf-8")
    assert suggest_compact.read_and_increment(invalid_counter) == 1

    counter_file = tmp_path / "claude-tool-count-session-123"
    counter_file.write_text("26", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_SESSION_ID", "session-123")
    monkeypatch.setenv("COMPACT_THRESHOLD", "2")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    stderr = io.StringIO()
    with redirect_stderr(stderr):
        assert suggest_compact.main() == 0

    assert "27 tool calls" in stderr.getvalue()

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.hooks.suggest_compact", run_name="__main__")

    assert excinfo.value.code == 0


class TestImportAdrsAndInstincts:
    def test_success_direct(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """_import_adrs_and_instincts の正常系: DB 呼び出しと log を確認。"""
        logs: list[str] = []
        fake_db = SimpleNamespace(close=lambda: None)

        import devgear.hooks.session_start as ss_mod
        import devgear.mem.importers as importers_mod
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(ss_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "devgear.hooks.session_start.get_git_user_name", lambda: "user", raising=False
        )
        monkeypatch.setattr(importers_mod, "import_instincts", lambda db, user: 3)
        monkeypatch.setattr(importers_mod, "import_adrs", lambda db, user, repo_root: 2)

        import devgear.mem.database as db_mod

        monkeypatch.setattr(db_mod, "Database", lambda path: fake_db)

        ss_mod._import_adrs_and_instincts()

        assert any("instincts=3" in msg and "adrs=2" in msg for msg in logs)

    def test_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """_import_adrs_and_instincts の異常系: 例外が log に記録される。"""
        logs: list[str] = []

        import devgear.hooks.session_start as ss_mod
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(ss_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("db-fail"))
        )

        ss_mod._import_adrs_and_instincts()

        assert any("mem import error" in msg for msg in logs)

    def test_session_start_run_calls_import(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """session_start.run() が _import_adrs_and_instincts を呼ぶことを確認。"""
        called: list[bool] = []

        import devgear.hooks.session_start as ss_mod

        monkeypatch.setattr(ss_mod, "ensure_dir", lambda path: None)
        monkeypatch.setattr(ss_mod, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(ss_mod, "get_sessions_dir", lambda: tmp_path / "sessions")
        monkeypatch.setattr(ss_mod, "get_session_search_dirs", lambda: [])
        monkeypatch.setattr(ss_mod, "find_files", lambda *args, **kwargs: [])
        monkeypatch.setattr(ss_mod, "get_package_manager", lambda: SimpleNamespace(name=None, source="auto"))
        monkeypatch.setattr(
            ss_mod,
            "detect_project",
            lambda cwd: SimpleNamespace(languages=[], frameworks=[], primary_language=None),
        )
        monkeypatch.setattr(ss_mod, "_save_project_profile", lambda info: None)
        monkeypatch.setattr(ss_mod, "_import_adrs_and_instincts", lambda: called.append(True))
        monkeypatch.setattr(
            ss_mod.Settings,
            "load",
            lambda: SimpleNamespace(slim=SimpleNamespace(enabled=False)),
        )
        monkeypatch.setattr(ss_mod, "log", lambda msg: None)

        ss_mod.run("{}")

        assert called == [True]


class TestRecordStopEvent:
    def test_success(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """_record_stop_event の正常系: store_event_log が呼ばれる。"""
        logs: list[str] = []
        stored: list[object] = []
        fake_db = SimpleNamespace(store_event_log=stored.append, close=lambda: None)

        import devgear.hooks.session_end as se_mod
        import devgear.mem.database as db_mod
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "devgear.hooks.session_end.get_git_user_name", lambda: "user", raising=False
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

        import devgear.hooks.session_end as se_mod
        import devgear.mem.database as db_mod
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", lambda msg: None)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: SimpleNamespace(db_path=tmp_path / "mem.db")
        )
        monkeypatch.setattr(
            "devgear.hooks.session_end.get_git_user_name", lambda: "user", raising=False
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

        import devgear.hooks.session_end as se_mod
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(se_mod, "log", logs.append)
        monkeypatch.setattr(
            settings_mod.Settings, "load", lambda: (_ for _ in ()).throw(RuntimeError("fail"))
        )

        se_mod._record_stop_event(None, {"project": "p"})

        assert any("event_log error" in msg for msg in logs)

    def test_session_end_run_calls_record_event(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """session_end.run() が _record_stop_event を呼ぶことを確認。"""
        called: list[tuple] = []

        import devgear.hooks.session_end as se_mod

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
