"""bluecore.mem.digest のテスト"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bluecore.mem.digest import (
    _aggregate_chunks,
    _extract_final_assistant_text,
    _truncate_summary,
    build_session_digest,
    generate_and_store_digest,
    resolve_transcript_path,
)
from bluecore.mem.models import InteractionLog, MemoryChunk, SessionDigest


def _make_chunk(
    *,
    session_id: str = "sess-1",
    project: str = "proj",
    chunk_index: int = 0,
    content: str = "content",
    tool_names: list[str] | None = None,
    files_modified: list[str] | None = None,
    user_prompt: str = "prompt",
    created_at_epoch: int = 1700000000,
    execution_status: str = "unknown",
) -> MemoryChunk:
    """テスト用の MemoryChunk を構築する。"""
    return MemoryChunk(
        session_id=session_id,
        project=project,
        chunk_index=chunk_index,
        content=content,
        tool_names=tool_names or [],
        files_read=[],
        files_modified=files_modified or [],
        user_prompt=user_prompt,
        created_at_epoch=created_at_epoch,
        execution_status=execution_status,
    )


def _make_log(prompt: str, index: int, epoch: int = 1700000000) -> InteractionLog:
    """テスト用の InteractionLog を構築する。"""
    return InteractionLog(
        session_id="sess-1",
        project="proj",
        user_prompt_full=prompt,
        interaction_index=index,
        created_at_epoch=epoch,
    )


class TestAggregateChunksOutcome:
    """_aggregate_chunks の outcome デシジョンテーブル"""

    @pytest.mark.parametrize(
        "statuses, expected",
        [
            ([], "unknown"),
            (["unknown", "unknown"], "unknown"),
            (["success"], "success"),
            (["success", "success"], "success"),
            (["failure"], "failure"),
            (["partial"], "failure"),  # partial 単独 → 比率0.5 → failure（境界値）
            (["success", "success", "partial"], "partial"),  # 比率 1/6 ≒ 0.167
            (["success", "success", "success", "failure"], "partial"),  # 比率 0.25
            (["success", "failure"], "failure"),  # 比率 0.5（境界値）
            (["failure", "failure", "success", "success"], "failure"),  # 比率 0.5
            (["failure", "unknown", "unknown"], "failure"),  # unknown は分母から除外
        ],
        ids=[
            "empty",
            "all-unknown",
            "single-success",
            "all-success",
            "single-failure",
            "single-partial-boundary",
            "mostly-success-one-partial",
            "mostly-success-one-failure",
            "half-failure-boundary",
            "half-failure-four",
            "unknown-excluded-from-denominator",
        ],
    )
    def test_outcome(self, statuses: list[str], expected: str) -> None:
        chunks = [_make_chunk(execution_status=s, chunk_index=i) for i, s in enumerate(statuses)]
        result = _aggregate_chunks(chunks, [])
        assert result.outcome == expected


class TestAggregateChunksKeyFiles:
    """_aggregate_chunks の key_files 集約テスト"""

    def test_top10_ordered_by_count_desc_then_path_asc(self) -> None:
        chunks = [
            _make_chunk(chunk_index=0, files_modified=["b.py", "a.py"]),
            _make_chunk(chunk_index=1, files_modified=["b.py"]),
            _make_chunk(chunk_index=2, files_modified=["c.py"]),
        ]
        result = _aggregate_chunks(chunks, [])
        # b.py: count=2, a.py: count=1, c.py: count=1 → 同数は path 昇順
        assert result.key_files == ["b.py", "a.py", "c.py"]

    def test_limited_to_top10(self) -> None:
        chunks = [_make_chunk(chunk_index=i, files_modified=[f"f{i:02d}.py"]) for i in range(15)]
        result = _aggregate_chunks(chunks, [])
        assert len(result.key_files) == 10
        assert result.key_files == [f"f{i:02d}.py" for i in range(10)]

    def test_empty_files_modified(self) -> None:
        result = _aggregate_chunks([_make_chunk()], [])
        assert result.key_files == []


class TestAggregateChunksKeyDecisions:
    """_aggregate_chunks の key_decisions 集約テスト"""

    def test_uses_interaction_logs_when_available(self) -> None:
        logs = [_make_log(f"decision {i}", i) for i in range(3)]
        chunks = [_make_chunk(user_prompt="ignored")]
        result = _aggregate_chunks(chunks, logs)
        assert result.key_decisions == ["decision 0", "decision 1", "decision 2"]

    def test_falls_back_to_chunk_user_prompt(self) -> None:
        chunks = [_make_chunk(chunk_index=i, user_prompt=f"prompt {i}") for i in range(3)]
        result = _aggregate_chunks(chunks, [])
        assert result.key_decisions == ["prompt 0", "prompt 1", "prompt 2"]

    def test_dedupes_preserving_order(self) -> None:
        logs = [_make_log("same", 0), _make_log("same", 1), _make_log("other", 2)]
        result = _aggregate_chunks([], logs)
        assert result.key_decisions == ["same", "other"]

    def test_first_and_last_four_when_more_than_five(self) -> None:
        logs = [_make_log(f"d{i}", i) for i in range(8)]
        result = _aggregate_chunks([], logs)
        assert result.key_decisions == ["d0", "d4", "d5", "d6", "d7"]

    def test_truncates_to_120_chars(self) -> None:
        long_prompt = "x" * 200
        logs = [_make_log(long_prompt, 0)]
        result = _aggregate_chunks([], logs)
        assert len(result.key_decisions[0]) <= 123  # compact_line は "..." (3字) 付与を許容
        assert result.key_decisions[0].endswith("...")


class TestExtractFinalAssistantText:
    """_extract_final_assistant_text のトランスクリプト解析テスト"""

    def _write_jsonl(self, tmp_path: Path, lines: list[str]) -> Path:
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    def test_missing_file_returns_none(self, tmp_path: Path) -> None:
        assert _extract_final_assistant_text(tmp_path / "missing.jsonl", "claude") is None

    def test_oversized_file_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import bluecore.mem.digest as digest_mod

        monkeypatch.setattr(digest_mod, "_MAX_TRANSCRIPT_BYTES", 10)
        path = tmp_path / "big.jsonl"
        path.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}), encoding="utf-8")
        assert _extract_final_assistant_text(path, "claude") is None

    def test_claude_format_returns_last_non_empty(self, tmp_path: Path) -> None:
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "first response"}]}}),
            json.dumps({"type": "user", "message": {"content": "user says hi"}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": ""}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "final response"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") == "final response"

    def test_codex_format_uses_same_shape_as_claude(self, tmp_path: Path) -> None:
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "codex says hi"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "codex") == "codex says hi"

    def test_copilot_format_returns_last_non_empty(self, tmp_path: Path) -> None:
        lines = [
            json.dumps({"type": "assistant.message", "data": {"content": "copilot first"}}),
            json.dumps({"type": "assistant.message", "data": {"content": ""}}),
            json.dumps({"type": "assistant.message", "data": {"content": "copilot final"}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "copilot") == "copilot final"

    def test_broken_json_line_is_skipped(self, tmp_path: Path) -> None:
        lines = [
            "not valid json {{{",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok response"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") == "ok response"

    def test_no_assistant_entries_returns_none(self, tmp_path: Path) -> None:
        lines = [json.dumps({"type": "user", "message": {"content": "hi"}})]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") is None

    def test_empty_lines_are_skipped(self, tmp_path: Path) -> None:
        lines = [
            "",
            "   ",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") == "ok"

    def test_claude_entry_with_non_list_content_is_ignored(self, tmp_path: Path) -> None:
        lines = [
            json.dumps({"type": "assistant", "message": {"content": "not a list"}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "fallback"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") == "fallback"

    def test_copilot_ignores_non_matching_entry_type(self, tmp_path: Path) -> None:
        lines = [
            json.dumps({"type": "user.message", "data": {"content": "user text"}}),
            json.dumps({"type": "assistant.message", "data": {"content": "final"}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "copilot") == "final"

    def test_non_dict_json_line_is_skipped(self, tmp_path: Path) -> None:
        lines = [
            json.dumps([1, 2, 3]),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}),
        ]
        path = self._write_jsonl(tmp_path, lines)
        assert _extract_final_assistant_text(path, "claude") == "ok"

    def test_os_error_on_stat_returns_none(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "t.jsonl"
        path.write_text("{}", encoding="utf-8")

        def _raise_os_error(self: Path) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "stat", _raise_os_error)
        assert _extract_final_assistant_text(path, "claude") is None


class TestTruncateSummary:
    """_truncate_summary の文字数制御テスト"""

    def test_short_text_unchanged_after_whitespace_compaction(self) -> None:
        assert _truncate_summary("  hello   world  ") == "hello world"

    def test_returns_as_is_when_within_limit(self) -> None:
        text = "a" * 500
        assert _truncate_summary(text) == text

    def test_cuts_at_sentence_boundary_within_window(self) -> None:
        # 420文字目に句点、その後490文字まで文字が続く（合計500文字超）
        text = "a" * 420 + "。" + "b" * 490
        result = _truncate_summary(text)
        assert result == "a" * 420 + "。"
        assert not result.endswith("…")

    def test_cuts_at_newline_boundary_within_window(self) -> None:
        text = "a" * 450 + "\n" + "b" * 490
        result = _truncate_summary(text)
        assert result == "a" * 450
        assert not result.endswith("…")

    def test_hard_cut_with_ellipsis_when_no_boundary(self) -> None:
        text = "a" * 600
        result = _truncate_summary(text)
        assert result == "a" * 500 + "…"

    def test_custom_max_chars(self) -> None:
        text = "a" * 150
        result = _truncate_summary(text, max_chars=100)
        assert result == "a" * 100 + "…"


class TestBuildSessionDigest:
    """build_session_digest の統合テスト"""

    def test_returns_none_when_no_chunks(self) -> None:
        class EmptyDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return []

        assert build_session_digest(EmptyDB(), "sess-1") is None

    def test_falls_back_to_rule_based_summary_without_transcript(self) -> None:
        chunks = [
            _make_chunk(
                chunk_index=0,
                tool_names=["Edit"],
                files_modified=["a.py"],
                user_prompt="fix the bug in auth module",
                created_at_epoch=1700000000,
                execution_status="success",
            ),
            _make_chunk(
                chunk_index=1,
                tool_names=["Bash"],
                files_modified=["b.py"],
                created_at_epoch=1700000100,
                execution_status="success",
            ),
        ]

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

        digest = build_session_digest(FakeDB(), "sess-1")
        assert digest is not None
        assert digest.source == "chunks"
        assert digest.outcome == "success"
        assert digest.chunk_count == 2
        assert digest.started_at_epoch == 1700000000
        assert digest.ended_at_epoch == 1700000100
        assert "目的:" in digest.summary
        assert "結果:success" in digest.summary
        assert "主変更:" in digest.summary
        assert "ツール:" in digest.summary

    def test_uses_transcript_when_available(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "transcript summary text"}]}}),
            encoding="utf-8",
        )
        chunks = [_make_chunk()]

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

        digest = build_session_digest(FakeDB(), "sess-1", transcript_path=str(transcript), harness="claude")
        assert digest is not None
        assert digest.source == "transcript+chunks"
        assert digest.summary == "transcript summary text"
        assert digest.harness == "claude"

    def test_missing_transcript_path_falls_back_to_chunks(self) -> None:
        chunks = [_make_chunk()]

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

        digest = build_session_digest(FakeDB(), "sess-1", transcript_path="/nonexistent/path.jsonl")
        assert digest is not None
        assert digest.source == "chunks"

    def test_applies_redaction_to_summary(self, tmp_path: Path) -> None:
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "contact me at leak@example.com please"}]}}),
            encoding="utf-8",
        )
        chunks = [_make_chunk()]

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

        digest = build_session_digest(FakeDB(), "sess-1", transcript_path=str(transcript), harness="claude")
        assert digest is not None
        assert "leak@example.com" not in digest.summary
        assert "[REDACTED]" in digest.summary


class TestGenerateAndStoreDigest:
    """generate_and_store_digest の統合テスト"""

    def test_stores_digest_when_chunks_exist(self) -> None:
        chunks = [_make_chunk()]
        stored: list[SessionDigest] = []

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

            def upsert_session_digest(self, digest: SessionDigest) -> str:
                stored.append(digest)
                return "digest-1"

        generate_and_store_digest(FakeDB(), "sess-1", transcript_path=None, log=_NullLog())
        assert len(stored) == 1
        assert stored[0].session_id == "sess-1"

    def test_no_op_when_no_chunks(self) -> None:
        stored: list[SessionDigest] = []

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return []

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

            def upsert_session_digest(self, digest: SessionDigest) -> str:
                stored.append(digest)
                return "digest-1"

        generate_and_store_digest(FakeDB(), "sess-1", transcript_path=None, log=_NullLog())
        assert stored == []

    def test_uses_detect_harness_when_transcript_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import bluecore.mem.digest as digest_mod

        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}),
            encoding="utf-8",
        )
        chunks = [_make_chunk()]
        stored: list[SessionDigest] = []

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

            def upsert_session_digest(self, digest: SessionDigest) -> str:
                stored.append(digest)
                return "digest-1"

        monkeypatch.setattr(digest_mod, "detect_harness", lambda: "codex")
        generate_and_store_digest(FakeDB(), "sess-1", transcript_path=str(transcript), log=_NullLog())
        assert stored[0].harness == "codex"

    def test_harness_unknown_without_transcript(self) -> None:
        chunks = [_make_chunk()]
        stored: list[SessionDigest] = []

        class FakeDB:
            def get_chunks_by_session(self, session_id: str) -> list[MemoryChunk]:
                return chunks

            def get_interaction_logs(self, session_id: str) -> list[InteractionLog]:
                return []

            def upsert_session_digest(self, digest: SessionDigest) -> str:
                stored.append(digest)
                return "digest-1"

        generate_and_store_digest(FakeDB(), "sess-1", transcript_path=None, log=_NullLog())
        assert stored[0].harness == "unknown"


class _NullLog:
    """テスト用の何もしないロガースタブ。"""

    def info(self, *args: object, **kwargs: object) -> None:
        """info ログを無視する。"""

    def warning(self, *args: object, **kwargs: object) -> None:
        """warning ログを無視する。"""


class TestResolveTranscriptPath:
    """resolve_transcript_path の3分岐テスト（digest-backfill 用）。"""

    def _patch_home(self, monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
        """Path.home() をテスト用ディレクトリに固定する。"""
        monkeypatch.setattr(Path, "home", lambda: home)

    def test_claude_transcript_found(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_home(monkeypatch, tmp_path)
        project_dir = tmp_path / ".claude" / "projects" / "my-project"
        project_dir.mkdir(parents=True)
        transcript = project_dir / "sess-123.jsonl"
        transcript.write_text("{}", encoding="utf-8")

        path, harness = resolve_transcript_path("sess-123")

        assert path == transcript
        assert harness == "claude"

    def test_copilot_transcript_found_when_claude_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._patch_home(monkeypatch, tmp_path)
        session_dir = tmp_path / ".copilot" / "session-state" / "sess-456"
        session_dir.mkdir(parents=True)
        events = session_dir / "events.jsonl"
        events.write_text("{}", encoding="utf-8")

        path, harness = resolve_transcript_path("sess-456")

        assert path == events
        assert harness == "copilot"

    def test_neither_found_returns_unknown(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._patch_home(monkeypatch, tmp_path)

        path, harness = resolve_transcript_path("sess-missing")

        assert path is None
        assert harness == "unknown"

    def test_claude_takes_precedence_over_copilot(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """claude と copilot 両方に存在する場合、claude が優先される。"""
        self._patch_home(monkeypatch, tmp_path)
        project_dir = tmp_path / ".claude" / "projects" / "proj"
        project_dir.mkdir(parents=True)
        claude_transcript = project_dir / "sess-both.jsonl"
        claude_transcript.write_text("{}", encoding="utf-8")

        copilot_dir = tmp_path / ".copilot" / "session-state" / "sess-both"
        copilot_dir.mkdir(parents=True)
        (copilot_dir / "events.jsonl").write_text("{}", encoding="utf-8")

        path, harness = resolve_transcript_path("sess-both")

        assert path == claude_transcript
        assert harness == "claude"

    def test_multiple_claude_hits_returns_first_sorted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """複数プロジェクトディレクトリに同一 session_id が存在する場合、先頭（ソート順）の1件を返す。"""
        self._patch_home(monkeypatch, tmp_path)
        dir_b = tmp_path / ".claude" / "projects" / "project-b"
        dir_a = tmp_path / ".claude" / "projects" / "project-a"
        dir_b.mkdir(parents=True)
        dir_a.mkdir(parents=True)
        (dir_b / "sess-dup.jsonl").write_text("{}", encoding="utf-8")
        (dir_a / "sess-dup.jsonl").write_text("{}", encoding="utf-8")

        path, harness = resolve_transcript_path("sess-dup")

        assert path == dir_a / "sess-dup.jsonl"
        assert harness == "claude"
