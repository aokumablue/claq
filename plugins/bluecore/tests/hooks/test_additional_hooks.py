"""低カバレッジの hook モジュールをまとめて検証するテスト。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

import pytest

from bluecore.hooks import (
    block_no_verify,
    cost_tracker,
    evaluate_session,
    post_bash_build_complete,
    pre_bash_git_push_reminder,
    pre_compact,
    session_end,
    session_end_marker,
)


def _capture_io(monkeypatch: pytest.MonkeyPatch, module, payload: str) -> tuple[list[str], list[str]]:
    stdout: list[str] = []
    stderr: list[str] = []
    monkeypatch.setattr(module, "read_raw_stdin", lambda: payload)
    if hasattr(module, "write_stdout"):
        monkeypatch.setattr(module, "write_stdout", stdout.append)
    if hasattr(module, "write_stderr"):
        monkeypatch.setattr(module, "write_stderr", stderr.append)
    return stdout, stderr


def _run_entrypoint(module_name: str) -> int:
    """モジュールを __main__ として実行し、終了コードを返す。"""
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module(module_name, run_name="__main__")

    return excinfo.value.code


class TestBlockNoVerify:
    @pytest.mark.parametrize(
        ("command", "expected_code", "blocked"),
        [
            ("git commit --no-verify", 2, True),
            ("git push -n", 2, True),
            ("git status", 0, False),
        ],
    )
    def test_main(self, monkeypatch: pytest.MonkeyPatch, command: str, expected_code: int, blocked: bool) -> None:
        payload = json.dumps({"tool_input": {"command": command}})
        stdout, stderr = _capture_io(monkeypatch, block_no_verify, payload)

        assert block_no_verify.main() == expected_code
        assert stdout == []
        assert bool(stderr) is blocked


class TestGitPushReminder:
    def test_git_push_triggers_warning(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps({"tool_input": {"command": "git push origin main"}})
        stdout, stderr = _capture_io(monkeypatch, pre_bash_git_push_reminder, payload)

        assert pre_bash_git_push_reminder.main() == 0
        assert stdout == []
        assert any("Review changes before push" in message for message in stderr)

    def test_non_push_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps({"tool_input": {"command": "git commit -m 'test'"}})
        stdout, stderr = _capture_io(monkeypatch, pre_bash_git_push_reminder, payload)

        assert pre_bash_git_push_reminder.main() == 0
        assert stdout == []
        assert stderr == []


class TestBuildComplete:
    def test_build_command_triggers_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps({"tool_input": {"command": "npm run build"}})
        stdout, stderr = _capture_io(monkeypatch, post_bash_build_complete, payload)

        assert post_bash_build_complete.main() == 0
        assert stdout == []
        assert any("Build completed" in message for message in stderr)

    def test_non_build_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = json.dumps({"tool_input": {"command": "npm test"}})
        stdout, stderr = _capture_io(monkeypatch, post_bash_build_complete, payload)

        assert post_bash_build_complete.main() == 0
        assert stdout == []
        assert stderr == []


class TestSessionEndMarker:
    def test_main_passes_input_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = '{"session": "end"}'
        stdout, stderr = _capture_io(monkeypatch, session_end_marker, payload)

        assert session_end_marker.main() == 0
        assert stdout == []
        assert stderr == []


class TestCostTracker:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("3", 3.0),
            ("3.5", 3.5),
            (None, 0),
            ("nan", 0),
            (float("inf"), 0),
        ],
    )
    def test_to_number(self, value: object, expected: int | float) -> None:
        assert cost_tracker.to_number(value) == expected

    @pytest.mark.parametrize(
        ("model", "input_tokens", "output_tokens", "expected"),
        [
            ("haiku", 1_000_000, 1_000_000, 4.8),
            ("sonnet", 1_000_000, 1_000_000, 18.0),
            ("opus", 1_000_000, 1_000_000, 90.0),
            ("unknown", 1_000_000, 1_000_000, 18.0),
        ],
    )
    def test_estimate_cost(self, model: str, input_tokens: int, output_tokens: int, expected: float) -> None:
        assert cost_tracker.estimate_cost(model, input_tokens, output_tokens) == expected

    def test_main_writes_metrics_row(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        payload = json.dumps(
            {
                "model": "haiku",
                "usage": {"input_tokens": 12, "output_tokens": 34},
            }
        )
        appended: list[tuple[Path, str]] = []

        monkeypatch.setattr(cost_tracker, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(cost_tracker, "append_file", lambda path, content: appended.append((Path(path), content)))
        monkeypatch.setattr(cost_tracker, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(cost_tracker, "get_bluecore_dir", lambda: tmp_path)
        monkeypatch.setenv("CLAUDE_SESSION_ID", "session-123")

        assert cost_tracker.main() == 0
        assert appended[0][0] == tmp_path / "metrics" / "costs.jsonl"

        row = json.loads(appended[0][1].strip())
        assert row["session_id"] == "session-123"
        assert row["model"] == "haiku"
        assert row["input_tokens"] == 12
        assert row["output_tokens"] == 34
        assert row["estimated_cost_usd"] == cost_tracker.estimate_cost("haiku", 12, 34)

    def test_main_entrypoint_emits_no_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")
        monkeypatch.setattr("bluecore.hooks.hook_common.parse_json_object", lambda raw: None)
        outputs: list[str] = []
        monkeypatch.setattr("bluecore.hooks.hook_common.write_stdout", outputs.append)

        assert _run_entrypoint("bluecore.hooks.cost_tracker") == 0
        assert outputs == []


class TestEvaluateSession:
    def test_main_skips_when_transcript_is_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        payload = json.dumps({})
        logs: list[str] = []
        monkeypatch.setattr(evaluate_session, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(evaluate_session, "log", logs.append)
        monkeypatch.setattr(evaluate_session, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(evaluate_session, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(
            evaluate_session,
            "read_file",
            lambda path: json.dumps({"min_session_length": 2}) if Path(path).name == "config.json" else None,
        )
        monkeypatch.setattr(evaluate_session, "_default_config_path", lambda: tmp_path / "config.json")

        assert evaluate_session.main() == 0
        assert logs == []

    def test_main_uses_custom_learned_skills_path_and_short_session(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.json"
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type":"user","content":"hello"}\n', encoding="utf-8")
        config_path.write_text(
            json.dumps({"min_session_length": 2, "learned_skills_path": "~/learned"}),
            encoding="utf-8",
        )

        logs: list[str] = []
        ensured: list[Path] = []

        monkeypatch.setattr(evaluate_session, "read_raw_stdin", lambda: json.dumps({"transcript_path": str(transcript_path)}))
        monkeypatch.setattr(evaluate_session, "log", logs.append)
        monkeypatch.setattr(evaluate_session, "ensure_dir", lambda path: ensured.append(Path(path)))
        monkeypatch.setattr(evaluate_session.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(evaluate_session, "_default_config_path", lambda: config_path)

        assert evaluate_session.main() == 0
        assert ensured == [tmp_path / "learned"]
        assert any("Session too short" in message for message in logs)

    def test_main_logs_invalid_config_and_continues_with_defaults(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        config_path = tmp_path / "config.json"
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type":"user","content":"hello"}\n', encoding="utf-8")

        logs: list[str] = []
        monkeypatch.setattr(
            evaluate_session,
            "read_raw_stdin",
            lambda: json.dumps({"transcript_path": str(transcript_path)}),
        )
        monkeypatch.setattr(evaluate_session, "log", logs.append)
        monkeypatch.setattr(evaluate_session, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(evaluate_session, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(evaluate_session, "read_file", lambda path: "not-json" if Path(path) == config_path else None)
        monkeypatch.setattr(evaluate_session, "_default_config_path", lambda: config_path)

        assert evaluate_session.main() == 0
        assert any("Failed to parse config" in message for message in logs)

    def test_main_logs_outer_exception(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text('{"type":"user","content":"hello"}\n', encoding="utf-8")

        logs: list[str] = []
        monkeypatch.setattr(
            evaluate_session,
            "read_raw_stdin",
            lambda: json.dumps({"transcript_path": str(transcript_path)}),
        )
        monkeypatch.setattr(evaluate_session, "log", logs.append)
        monkeypatch.setattr(evaluate_session, "read_file", lambda path: None)

        def fail_ensure_dir(path):  # noqa: ANN001
            raise RuntimeError("boom")

        monkeypatch.setattr(evaluate_session, "ensure_dir", fail_ensure_dir)
        monkeypatch.setattr(evaluate_session, "get_learned_skills_dir", lambda: tmp_path / "learned")

        assert evaluate_session.main() == 0
        assert any("Error: boom" in message for message in logs)

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")
        monkeypatch.setattr("bluecore.lib.core_utils.read_file", lambda path: None)
        monkeypatch.setattr("bluecore.lib.core_utils.get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr("bluecore.lib.core_utils.ensure_dir", lambda path: Path(path))
        monkeypatch.setattr("bluecore.lib.core_utils.log", lambda message: None)

        assert _run_entrypoint("bluecore.hooks.evaluate_session") == 0

    def test_main_reports_long_sessions(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        config_path = tmp_path / "config.json"
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "content": "one"}),
                    json.dumps({"type": "user", "content": "two"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        config_path.write_text(
            json.dumps({"min_session_length": 1, "learned_skills_path": str(tmp_path / "learned")}),
            encoding="utf-8",
        )

        logs: list[str] = []
        ensured: list[Path] = []

        monkeypatch.setattr(
            evaluate_session,
            "read_raw_stdin",
            lambda: json.dumps({"transcript_path": str(transcript_path)}),
        )
        monkeypatch.setattr(evaluate_session, "log", logs.append)
        monkeypatch.setattr(evaluate_session, "ensure_dir", lambda path: ensured.append(Path(path)))
        monkeypatch.setattr(evaluate_session, "read_file", lambda path: config_path.read_text(encoding="utf-8"))
        monkeypatch.setattr(evaluate_session, "_default_config_path", lambda: config_path)

        assert evaluate_session.main() == 0
        assert ensured == [tmp_path / "learned"]
        assert any("Session has 2 messages" in message for message in logs)
        assert any("Save learned skills to" in message for message in logs)


class TestSessionEndHelpers:
    def test_get_session_metadata_uses_git_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "get_project_name", lambda: "repo")
        monkeypatch.setattr(session_end, "run_command", lambda cmd: {"success": True, "output": "feature/test"})
        monkeypatch.setattr(session_end.Path, "cwd", lambda: Path("/worktree"))

        assert session_end.get_session_metadata() == {
            "project": "repo",
            "branch": "feature/test",
            "worktree": "/worktree",
        }

    def test_get_session_metadata_falls_back_when_branch_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "get_project_name", lambda: None)
        monkeypatch.setattr(session_end, "run_command", lambda cmd: {"success": False, "output": ""})
        monkeypatch.setattr(session_end.Path, "cwd", lambda: Path("/worktree"))

        assert session_end.get_session_metadata()["branch"] == "unknown"

    def test_build_summary_section_and_block(self) -> None:
        summary = {
            "userMessages": ["ご質問ありがとうございます。  Fix `docs`\nnow"],
            "filesModified": ["README.md"],
            "toolsUsed": ["Write"],
            "totalMessages": 1,
        }

        section = session_end.build_summary_section(summary)
        block = session_end.build_summary_block(summary)

        assert "ご質問ありがとうございます。" not in section
        assert "Fix \\`docs\\` now" in section
        assert "### Files Modified" in section
        assert "### 使用したツール" in section
        assert "### 統計" in section
        assert block.startswith(session_end.SUMMARY_START_MARKER)
        assert block.endswith(session_end.SUMMARY_END_MARKER)

    def test_extract_session_summary_compacts_user_messages(self, tmp_path: Path) -> None:
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "content": "ご質問ありがとうございます。  修正お願いします。"}),
                    json.dumps({"type": "user", "content": "   second line"}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        summary = session_end.extract_session_summary(str(transcript))

        assert summary is not None
        assert summary["userMessages"] == ["修正お願いします", "second line"]

    def test_merge_session_header_handles_separator(self) -> None:
        content = "# Session: old\n**Date:** 2025-01-01\n**Started:** 09:00\n---\nbody"
        merged = session_end.merge_session_header(
            content,
            today="2026-01-01",
            current_time="10:00",
            metadata={"project": "repo", "branch": "main", "worktree": "/worktree"},
        )

        assert merged is not None
        assert "**Project:** repo" in merged
        assert "**Last Updated:** 10:00" in merged
        assert merged.endswith("body")

    def test_merge_session_header_returns_none_without_separator(self) -> None:
        assert (
            session_end.merge_session_header(
                "# Session: old",
                today="2026-01-01",
                current_time="10:00",
                metadata={"project": "repo", "branch": "main", "worktree": "/worktree"},
            )
            is None
        )

    def test_extract_session_summary_returns_none_for_empty_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_end, "read_file", lambda path: "")

        assert session_end.extract_session_summary("missing.jsonl") is None

    def test_extract_session_summary_logs_parse_errors_when_no_user_messages(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        logs: list[str] = []
        content = "\n".join(
            [
                "not-json",
                json.dumps(
                    {
                        "type": "tool_use",
                        "tool_name": "Edit",
                        "tool_input": {"file_path": "README.md"},
                    }
                ),
            ]
        )
        monkeypatch.setattr(session_end, "read_file", lambda path: content)
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.extract_session_summary("transcript.jsonl") is None
        assert any("Skipped 1/2 unparseable transcript lines" in message for message in logs)


class TestSessionEndMain:
    def test_main_creates_session_file_with_summary(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("{}", encoding="utf-8")

        summary = {
            "userMessages": ["Fix docs"],
            "filesModified": ["README.md"],
            "toolsUsed": ["Write"],
            "totalMessages": 1,
        }
        writes: list[tuple[Path, str]] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: json.dumps({"transcript_path": str(transcript_path)}))
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"})
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "extract_session_summary", lambda path: summary)
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))

        assert session_end.main() == 0
        assert capsys.readouterr().out == ""
        assert writes[0][0] == sessions_dir / "2026-01-01-abc123-session.tmp"
        assert "Fix docs" in writes[0][1]
        assert "README.md" in writes[0][1]

    def test_main_updates_existing_session_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("{}", encoding="utf-8")
        session_file = sessions_dir / "2026-01-01-abc123-session.tmp"
        session_file.write_text(
            "\n".join(
                [
                    "# Session: old",
                    "**Date:** 2026-01-01",
                    "**Started:** 09:00",
                    "**Last Updated:** 09:00",
                    "**Project:** repo",
                    "**Branch:** main",
                    "**Worktree:** /repo",
                    "",
                ]
            )
            + session_end.SESSION_SEPARATOR
            + "\n".join(
                [
                    session_end.SUMMARY_START_MARKER,
                    "old summary",
                    session_end.SUMMARY_END_MARKER,
                    "old body",
                ]
            ),
            encoding="utf-8",
        )

        summary = {
            "userMessages": ["Fix docs"],
            "filesModified": ["README.md"],
            "toolsUsed": ["Write"],
            "totalMessages": 1,
        }
        writes: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: json.dumps({"transcript_path": str(transcript_path)}))
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"})
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "extract_session_summary", lambda path: summary)
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert capsys.readouterr().out == ""
        assert writes[0][0] == session_file
        assert "Fix docs" in writes[0][1]
        assert any(msg.startswith("[SessionEnd] Updated session file:") for msg in logs)

    def test_main_creates_default_template_when_transcript_is_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "missing-transcript.jsonl"
        writes: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: json.dumps({"transcript_path": str(transcript_path)}))
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"})
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert capsys.readouterr().out == ""
        assert any("Transcript not found" in message for message in logs)
        assert writes[0][0] == sessions_dir / "2026-01-01-abc123-session.tmp"
        assert "## 現在の状態" in writes[0][1]

    def test_main_logs_when_header_normalization_fails(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        session_file = sessions_dir / "2026-01-01-abc123-session.tmp"
        session_file.write_text("# Session: old\nbody", encoding="utf-8")
        writes: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: "{}")
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"})
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert capsys.readouterr().out == ""
        assert any("Failed to normalize header" in message for message in logs)
        assert writes[0][0] == session_file

    def test_main_migrates_legacy_summary_block(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text(
            "\n".join(
                [
                    json.dumps({"type": "user", "content": "Fix docs"}),
                    json.dumps({"type": "tool_use", "tool_name": "Write", "tool_input": {"file_path": "README.md"}}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        session_file = sessions_dir / "2026-01-01-abc123-session.tmp"
        session_file.write_text(
            "\n".join(
                [
                    "# Session: old",
                    "**Date:** 2026-01-01",
                    "**Started:** 09:00",
                    "**Last Updated:** 09:00",
                    "**Project:** repo",
                    "**Branch:** main",
                    "**Worktree:** /repo",
                    "",
                    "## Session Summary",
                    "old summary",
                ]
            ),
            encoding="utf-8",
        )
        writes: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: json.dumps({"transcript_path": str(transcript_path)}))
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"})
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert capsys.readouterr().out == ""
        assert session_end.SUMMARY_START_MARKER in writes[0][1]
        assert "### 次回セッションへの引継ぎ" in writes[0][1]
        assert any("Updated session file" in message for message in logs)

    def test_main_logs_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logs: list[str] = []

        monkeypatch.setattr(session_end, "read_raw_stdin", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.main() == 0
        assert any("Error: boom" in message for message in logs)

    def test_run_logs_on_outer_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        logs: list[str] = []
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(session_end, "log", logs.append)

        assert session_end.run("{}") == "{}"
        assert any("Error: boom" in message for message in logs)

    def test_run_skips_stop_event_during_loop_continuation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """stop_hook_active=True（/goal などのループ継続）では session_stop を記録しない。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("{}", encoding="utf-8")
        summary = {"userMessages": ["work"], "filesModified": [], "toolsUsed": [], "totalMessages": 1}
        writes: list[tuple[Path, str]] = []
        recorded: list[tuple] = []
        logs: list[str] = []

        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(
            session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"}
        )
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "extract_session_summary", lambda path: summary)
        monkeypatch.setattr(session_end, "write_file", lambda path, content: writes.append((Path(path), content)))
        monkeypatch.setattr(session_end, "_record_stop_event", lambda *args: recorded.append(args))
        monkeypatch.setattr(session_end, "log", logs.append)

        raw = json.dumps({"transcript_path": str(transcript_path), "stop_hook_active": True})
        assert session_end.run(raw) == raw
        # ループ継続中は session_stop を記録しない
        assert recorded == []
        # 継続用セッションファイルは引き続き更新される
        assert writes and writes[0][0] == sessions_dir / "2026-01-01-abc123-session.tmp"
        assert any("loop continuation" in message for message in logs)

    def test_run_records_stop_event_on_genuine_stop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """stop_hook_active が無い通常停止では session_stop を記録する。"""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        transcript_path = tmp_path / "transcript.jsonl"
        transcript_path.write_text("{}", encoding="utf-8")
        summary = {"userMessages": ["work"], "filesModified": [], "toolsUsed": [], "totalMessages": 1}
        recorded: list[tuple] = []

        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr(session_end, "get_session_id_short", lambda: "abc123")
        monkeypatch.setattr(
            session_end, "get_session_metadata", lambda: {"project": "repo", "branch": "main", "worktree": "/repo"}
        )
        monkeypatch.setattr(session_end, "get_time_string", lambda: "10:00")
        monkeypatch.setattr(session_end, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(session_end, "extract_session_summary", lambda path: summary)
        monkeypatch.setattr(session_end, "write_file", lambda path, content: None)
        monkeypatch.setattr(session_end, "_record_stop_event", lambda *args: recorded.append(args))
        monkeypatch.setattr(session_end, "log", lambda message: None)

        raw = json.dumps({"transcript_path": str(transcript_path)})
        assert session_end.run(raw) == raw
        assert len(recorded) == 1
        assert recorded[0][0] == summary

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")
        monkeypatch.setattr("bluecore.lib.core_utils.get_sessions_dir", lambda: tmp_path / "sessions")
        monkeypatch.setattr("bluecore.lib.core_utils.get_date_string", lambda: "2026-01-01")
        monkeypatch.setattr("bluecore.lib.core_utils.get_session_id_short", lambda: "abc123")
        monkeypatch.setattr("bluecore.lib.core_utils.get_project_name", lambda: "repo")
        monkeypatch.setattr("bluecore.lib.core_utils.run_command", lambda cmd: {"success": True, "output": "main"})
        monkeypatch.setattr("bluecore.lib.core_utils.get_time_string", lambda: "10:00")
        monkeypatch.setattr("bluecore.lib.core_utils.ensure_dir", lambda path: Path(path))
        monkeypatch.setattr("bluecore.lib.core_utils.write_file", lambda path, content: None)
        monkeypatch.setattr("bluecore.lib.core_utils.log", lambda message: None)

        assert _run_entrypoint("bluecore.hooks.session_end") == 0


class TestPreCompact:
    def test_main_logs_and_updates_active_session(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        session_file = tmp_path / "2026-01-01-abc-session.tmp"
        session_file.write_text("session", encoding="utf-8")

        appended: list[tuple[Path, str]] = []
        logs: list[str] = []

        monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(pre_compact, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(pre_compact, "append_file", lambda path, content: appended.append((Path(path), content)))
        monkeypatch.setattr(pre_compact, "find_files", lambda directory, pattern: [{"path": str(session_file)}])
        monkeypatch.setattr(pre_compact, "log", logs.append)
        monkeypatch.setattr(pre_compact, "get_datetime_string", lambda: "2026-01-01 00:00:00")
        monkeypatch.setattr(pre_compact, "get_time_string", lambda: "10:00")

        assert pre_compact.main() == 0
        assert appended[0][0] == tmp_path / "compaction-log.txt"
        assert "Context compaction triggered" in appended[0][1]
        assert appended[1][0] == session_file
        assert "Context was summarized" in appended[1][1]
        assert logs == ["[PreCompact] State saved before compaction"]

    def test_main_logs_on_exception(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        logs: list[str] = []
        monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr(pre_compact, "ensure_dir", lambda path: Path(path))
        monkeypatch.setattr(pre_compact, "append_file", lambda path, content: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(pre_compact, "find_files", lambda directory, pattern: [])
        monkeypatch.setattr(pre_compact, "log", logs.append)

        assert pre_compact.main() == 0
        assert any("Error: boom" in message for message in logs)

    def test_main_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr("bluecore.lib.core_utils.get_sessions_dir", lambda: tmp_path)
        monkeypatch.setattr("bluecore.lib.core_utils.ensure_dir", lambda path: Path(path))
        monkeypatch.setattr("bluecore.lib.core_utils.append_file", lambda path, content: None)
        monkeypatch.setattr("bluecore.lib.core_utils.find_files", lambda directory, pattern: [])
        monkeypatch.setattr("bluecore.lib.core_utils.get_datetime_string", lambda: "2026-01-01 00:00:00")
        monkeypatch.setattr("bluecore.lib.core_utils.get_time_string", lambda: "10:00")
        monkeypatch.setattr("bluecore.lib.core_utils.log", lambda message: None)

        assert _run_entrypoint("bluecore.hooks.pre_compact") == 0


class TestSimpleHookEntrypoints:
    def test_block_no_verify_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin",
            lambda: json.dumps({"tool_input": {"command": "git status"}}),
        )

        assert _run_entrypoint("bluecore.hooks.block_no_verify") == 0

    def test_git_push_reminder_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin",
            lambda: json.dumps({"tool_input": {"command": "git commit -m 'test'"}}),
        )

        assert _run_entrypoint("bluecore.hooks.pre_bash_git_push_reminder") == 0

    def test_build_complete_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "bluecore.hooks.hook_common.read_raw_stdin",
            lambda: json.dumps({"tool_input": {"command": "npm test"}}),
        )

        assert _run_entrypoint("bluecore.hooks.post_bash_build_complete") == 0

    def test_session_end_marker_entrypoint_exits_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: '{"session":"end"}')

        assert _run_entrypoint("bluecore.hooks.session_end_marker") == 0


class TestSessionStartRubyLog:
    """session_start フックが Ruby プロジェクトでログを出すことを確認するテスト。"""

    def test_ruby_project_emits_bundler_log(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Gemfile のみのプロジェクトで Ruby detected ログが出ること。"""
        from bluecore.hooks import session_start
        from bluecore.lib.package_manager import PackageManagerResult

        (tmp_path / "Gemfile").write_text('source "https://rubygems.org"\ngem "sinatra"\n', encoding="utf-8")

        logs: list[str] = []
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(session_start, "read_raw_stdin", lambda: "{}")
        monkeypatch.setattr(session_start, "log", logs.append)
        monkeypatch.setattr(session_start, "get_package_manager", lambda: PackageManagerResult(name=None, config=None, source="none"))
        monkeypatch.setattr(session_start, "ensure_dir", lambda _: None)
        monkeypatch.setattr(session_start, "find_files", lambda *_a, **_kw: [])
        monkeypatch.setattr(session_start, "_save_project_profile", lambda _: None)
        monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda _: None)

        output = session_start.run("{}")
        assert any("Ruby project detected" in msg for msg in logs), f"Expected Ruby log in: {logs}"
        assert "ruby" in output


class TestCheckpointInjection:
    """session_start がアクティブなチェックポイントを注入するテスト。"""

    def _make_session_start_base_patches(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """session_start.run() の共通モックを設定する。"""
        from bluecore.hooks import session_start
        from bluecore.lib.package_manager import PackageManagerResult

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(session_start, "log", lambda _: None)
        monkeypatch.setattr(session_start, "get_package_manager", lambda: PackageManagerResult(name=None, config=None, source="none"))
        monkeypatch.setattr(session_start, "ensure_dir", lambda _: None)
        monkeypatch.setattr(session_start, "_save_project_profile", lambda _: None)
        monkeypatch.setattr(session_start, "_import_adrs_and_instincts", lambda: None)
        monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda _: None)

    def test_active_checkpoint_is_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """completed: false のチェックポイントが additionalContext に注入されること。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-05-09-test.md"
        checkpoint.write_text(
            "---\ntask: test\ncompleted: false\n---\n\n## 目標\nテスト中\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [sessions_dir])
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Active checkpoint:" in context

    def test_active_checkpoint_is_capped_at_500_chars(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """チェックポイント注入は 500 文字上限に切り詰めること（注入トークン削減）。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-06-11-cap.md"
        checkpoint.write_text(
            "---\ntask: test\ncompleted: false\n---\n\n## 目標\n" + "ながいほんぶん " * 200 + "\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [sessions_dir])
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        marker = "Active checkpoint:\n"
        assert marker in context
        injected = context.split(marker, 1)[1].split("\n\n", 1)[0]
        assert len(injected) <= 510  # 500 + "..." 分のマージン

    def test_completed_checkpoint_is_not_injected(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """completed: true のチェックポイントは注入されないこと。"""
        from bluecore.hooks import session_start

        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        checkpoint = sessions_dir / "checkpoint-2026-05-09-done.md"
        checkpoint.write_text(
            "---\ntask: done\ncompleted: true\n---\n\n## 目標\n完了済み\n",
            encoding="utf-8",
        )

        self._make_session_start_base_patches(monkeypatch, tmp_path)
        monkeypatch.setattr(session_start, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_start, "get_session_search_dirs", lambda: [sessions_dir])
        monkeypatch.setattr(session_start, "get_learned_skills_dir", lambda: tmp_path / "learned")
        monkeypatch.setattr(session_start, "find_files", lambda path, pattern, **kw: (
            [{"path": str(checkpoint), "mtime": 9999}] if "checkpoint-" in pattern else []
        ))
        monkeypatch.setattr(session_start, "read_file", lambda p: checkpoint.read_text(encoding="utf-8") if str(p) == str(checkpoint) else "")

        output = session_start.run("{}")
        payload = json.loads(output)
        context = payload["hookSpecificOutput"]["additionalContext"]
        assert "Active checkpoint:" not in context


class TestAutoCheckpointSave:
    """session_end がメッセージ閾値超過時にチェックポイントを自動保存するテスト。"""

    def _make_summary(self, total_messages: int = 35) -> dict:
        """テスト用サマリーを生成する。"""
        return {
            "userMessages": [f"msg{i}" for i in range(min(total_messages, 10))],
            "toolsUsed": ["Edit", "Read"],
            "filesModified": ["path/to/file.py"],
            "totalMessages": total_messages,
        }

    def test_auto_checkpoint_created_when_threshold_exceeded(self, tmp_path: Path) -> None:
        """メッセージ数が閾値以上のとき新規チェックポイントが作成されること。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        metadata = {"project": "bluecore", "branch": "develop", "worktree": str(tmp_path)}
        summary = self._make_summary(35)

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        checkpoints = list(sessions_dir.glob("checkpoint-*.md"))
        assert len(checkpoints) == 1
        content = checkpoints[0].read_text(encoding="utf-8")
        assert "completed: false" in content
        assert "path/to/file.py" in content

    def test_auto_checkpoint_updates_existing(self, tmp_path: Path) -> None:
        """既存のアクティブなチェックポイントがある場合は更新されること。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        today = session_end.get_date_string()
        existing = sessions_dir / f"checkpoint-{today}-bluecore.md"
        existing.write_text(
            "---\ntask: bluecore\ncompleted: false\n---\n\n## 変更済みファイル\n- old.py\n\n## 再開コンテキスト\nold\n",
            encoding="utf-8",
        )
        metadata = {"project": "bluecore", "branch": "main", "worktree": str(tmp_path)}
        summary = self._make_summary(40)
        summary["filesModified"] = ["new.py"]

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        content = existing.read_text(encoding="utf-8")
        assert "new.py" in content
        assert "old.py" not in content

    def test_auto_checkpoint_skips_completed(self, tmp_path: Path) -> None:
        """completed: true の既存チェックポイントは更新しないこと。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        today = session_end.get_date_string()
        existing = sessions_dir / f"checkpoint-{today}-bluecore.md"
        existing.write_text(
            "---\ntask: bluecore\ncompleted: true\n---\n\n## 変更済みファイル\n- old.py\n",
            encoding="utf-8",
        )
        metadata = {"project": "bluecore", "branch": "main", "worktree": str(tmp_path)}
        summary = self._make_summary(40)

        session_end._auto_save_checkpoint(summary, metadata, sessions_dir)

        content = existing.read_text(encoding="utf-8")
        assert "completed: true" in content

    def test_run_triggers_checkpoint_when_threshold_met(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """run() がメッセージ数閾値以上のとき _auto_save_checkpoint を呼ぶこと。"""
        sessions_dir = tmp_path / "session-data"
        sessions_dir.mkdir()
        transcript = tmp_path / "transcript.jsonl"
        lines = [json.dumps({"type": "user", "content": f"msg{i}"}) for i in range(35)]
        transcript.write_text("\n".join(lines), encoding="utf-8")

        called: list[bool] = []

        def fake_auto_save(summary: dict, metadata: dict, sd: Path) -> None:
            called.append(True)

        monkeypatch.setattr(session_end, "_auto_save_checkpoint", fake_auto_save)
        monkeypatch.setattr(session_end, "get_sessions_dir", lambda: sessions_dir)
        monkeypatch.setattr(session_end, "_record_stop_event", lambda *_a: None)
        monkeypatch.setattr(session_end, "write_file", lambda *_a: None)
        monkeypatch.setattr(session_end, "ensure_dir", lambda _: None)

        raw = json.dumps({"transcript_path": str(transcript)})
        session_end.run(raw)

        assert called, "Expected _auto_save_checkpoint to be called"


class TestFilterSessionSummary:
    """_filter_session_summary のユニットテスト。"""

    _START = "<!-- bluecore:SUMMARY:START -->"
    _END = "<!-- bluecore:SUMMARY:END -->"

    def _wrap(self, body: str) -> str:
        return f"# Session: 2026-05-09\n---\n{self._START}\n{body}\n{self._END}\n### 次回セッションへの引継ぎ\n-\n### 読み込むコンテキスト\n```\n[relevant files]\n```\n"

    def test_keeps_only_tasks(self) -> None:
        """Tasks のみを保持し Files Modified は除外すること（注入トークン削減）。"""
        from bluecore.hooks.session_start import _filter_session_summary

        body = "### Tasks\n- msg1\n- msg2\n\n### Files Modified\n- foo.py\n\n### 使用したツール\nEdit, Read\n\n### 統計\n- ユーザーメッセージ総数: 5"
        result = _filter_session_summary(self._wrap(body))

        assert "### Tasks" in result
        assert "msg1" in result
        assert "### Files Modified" not in result
        assert "foo.py" not in result
        assert "使用したツール" not in result
        assert "統計" not in result

    def test_no_files_modified_section(self) -> None:
        """Files Modified がない場合は Tasks のみを返すこと。"""
        from bluecore.hooks.session_start import _filter_session_summary

        body = "### Tasks\n- only task\n\n### 使用したツール\nRead"
        result = _filter_session_summary(self._wrap(body))

        assert "### Tasks" in result
        assert "only task" in result
        assert "Files Modified" not in result

    def test_removes_template_sections(self) -> None:
        """テンプレート部分（引継ぎ・コンテキスト）を除外すること。"""
        from bluecore.hooks.session_start import _filter_session_summary

        body = "### Tasks\n- t1\n\n### Files Modified\n- a.py"
        result = _filter_session_summary(self._wrap(body))

        assert "次回セッションへの引継ぎ" not in result
        assert "読み込むコンテキスト" not in result
        assert "[relevant files]" not in result

    def test_truncates_when_exceeds_max_length(self) -> None:
        """2000文字を超える場合は compact_line で切り詰めること。"""
        from bluecore.hooks.session_start import _filter_session_summary

        long_tasks = "\n".join(f"- {'x' * 100}" for _ in range(30))
        body = f"### Tasks\n{long_tasks}\n\n### Files Modified\n- f.py"
        result = _filter_session_summary(self._wrap(body), max_length=2000)

        assert len(result) <= 2000
        assert result.endswith("...")

    def test_fallback_when_no_marker(self) -> None:
        """SUMMARY マーカーがない旧形式は compact_line にフォールバックすること。"""
        from bluecore.hooks.session_start import _filter_session_summary

        old_format = "## Session Summary\n- did something\n" * 50
        result = _filter_session_summary(old_format, max_length=2000)

        assert len(result) <= 2000

    def test_empty_content_returns_empty(self) -> None:
        """空文字列を渡すと空文字列を返すこと。"""
        from bluecore.hooks.session_start import _filter_session_summary

        assert _filter_session_summary("") == ""


@pytest.mark.parametrize(
    "module_name",
    [
        "block_no_verify",
        "doc_file_warning",
        "post_bash_build_complete",
        "config_protection",
        "pre_bash_git_push_reminder",
    ],
)
def test_empty_input_passthrough(monkeypatch: pytest.MonkeyPatch, module_name: str) -> None:
    """空入力（data None）では本処理をスキップして 0 を返す。"""
    import importlib

    mod = importlib.import_module(f"bluecore.hooks.{module_name}")
    _capture_io(monkeypatch, mod, "")
    assert mod.main() == 0


def test_config_protection_blank_file_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """file_path が空なら保護判定をスキップする。"""
    from bluecore.hooks import config_protection

    payload = json.dumps({"tool_input": {"file_path": ""}})
    _capture_io(monkeypatch, config_protection, payload)
    assert config_protection.main() == 0

def test_evaluate_session_config_without_min_length(tmp_path, monkeypatch) -> None:
    """設定に min_session_length が無ければ既定値 10 を維持する。"""
    monkeypatch.setattr(
        evaluate_session, "read_file", lambda path: json.dumps({"learned_skills_path": "~/x"})
    )
    min_len, _ = evaluate_session._load_learn_config(tmp_path / "config.json")
    assert min_len == 10


def test_collect_user_message_non_text_content() -> None:
    """content が文字列でもリストでもなければ空文字を返す。"""
    assert session_end._collect_user_message({"type": "user", "content": 123}) == ""


def test_collect_tool_use_empty_name_and_non_tool_block() -> None:
    """ツール名が空・非tool_useブロックでも例外なく走査する。"""
    tools: set[str] = set()
    files: set[str] = set()
    session_end._collect_tool_use({"type": "tool_use"}, tools, files)  # tool_name空・file無
    session_end._collect_tool_use(
        {"type": "assistant", "message": {"content": [{"type": "text"}, {"type": "tool_use", "name": "", "input": {}}]}},
        tools,
        files,
    )
    assert tools == set()
    assert files == set()


def test_build_summary_section_skips_empty_message() -> None:
    """空のユーザーメッセージはスキップする。"""
    summary = {"userMessages": ["", "real task"], "filesModified": [], "toolsUsed": [], "totalMessages": 2}
    out = session_end.build_summary_section(summary)
    assert "real task" in out


def test_update_session_file_no_existing_no_summary(monkeypatch, tmp_path: Path) -> None:
    """既存ファイルもサマリーも無ければ書き込みしない。"""
    monkeypatch.setattr(session_end, "read_file", lambda f: None)
    written: list[str] = []
    monkeypatch.setattr(session_end, "write_file", lambda f, c: written.append(c))
    session_end._update_session_file(tmp_path / "s.md", None, "2026-01-01", "00:00", {})
    assert written == []
