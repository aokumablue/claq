"""importers のテスト"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from devgear.mem.importers import (
    _get_project_identifier,
    _import_instincts_from_dir,
    _import_jsonl_events,
    _parse_adr_markdown,
    _parse_instinct_yaml,
    import_adrs,
)


class TestImportInstincts:
    """import_instincts のテスト"""

    def test_no_instincts_dir_returns_zero(self):
        from devgear.mem.importers import import_instincts

        db = MagicMock()
        # 存在しないパスをモック
        with patch("devgear.mem.importers.DEVGEAR_DIR", Path("/nonexistent")):
            result = import_instincts(db, "test_user")
        assert result == 0

    def test_parses_yaml_instinct_files(self, tmp_path):
        from devgear.mem.importers import import_instincts

        db = MagicMock()
        db.get_instinct_by_key.return_value = None  # 既存データなし

        # テスト用インスティンクトディレクトリを作成
        instincts_dir = tmp_path / ".devgear" / "instincts" / "personal"
        instincts_dir.mkdir(parents=True)

        # YAML ファイルを作成
        instinct_file = instincts_dir / "test-instinct.yaml"
        instinct_file.write_text(
            """---
id: test-instinct
trigger: when testing
confidence: 0.8
domain: testing
---
This is a test instinct.
"""
        )

        with patch("devgear.mem.importers.DEVGEAR_DIR", tmp_path / ".devgear"):
            result = import_instincts(db, "test_user")

        assert result == 1
        db.upsert_instinct.assert_called_once()

    def test_parses_frontmatter_plain_yaml_and_directory_import(self, tmp_path):
        instincts_dir = tmp_path / "instincts"
        instincts_dir.mkdir()
        frontmatter = instincts_dir / "frontmatter.yaml"
        frontmatter.write_text(
            """---
id: frontmatter-id
trigger: when testing
confidence: 0.8
domain: testing
---
This is a test instinct.
""",
            encoding="utf-8",
        )
        plain = instincts_dir / "plain.yml"
        plain.write_text(
            """id: plain-id
trigger: on plain yaml
confidence: 0.4
domain: linting
""",
            encoding="utf-8",
        )
        invalid = instincts_dir / "invalid.yaml"
        invalid.write_text("not: [valid", encoding="utf-8")
        list_yaml = instincts_dir / "list.yaml"
        list_yaml.write_text("- a\n- b\n", encoding="utf-8")

        parsed = _parse_instinct_yaml(frontmatter, "project", "proj-1", "user")
        assert parsed is not None
        assert parsed.instinct_id == "frontmatter-id"
        assert parsed.scope == "project"
        assert parsed.project_id == "proj-1"

        assert _parse_instinct_yaml(plain, "global", None, "user").instinct_id == "plain-id"
        assert _parse_instinct_yaml(invalid, "global", None, "user") is None
        assert _parse_instinct_yaml(list_yaml, "global", None, "user") is None

        empty_stem = instincts_dir / "empty.yaml"
        empty_stem.write_text("trigger: none\n", encoding="utf-8")
        with patch.object(Path, "stem", new=property(lambda self: "")):
            assert _parse_instinct_yaml(empty_stem, "global", None, "user") is None

        db = MagicMock()
        count = _import_instincts_from_dir(db, instincts_dir, "project", "proj-1", "user")
        assert count == 3
        assert db.upsert_instinct.call_count == 3

    def test_frontmatter_yaml_parse_error_falls_back_to_filename(self, tmp_path):
        file_path = tmp_path / "fallback.yaml"
        file_path.write_text(
            """---
id: [broken
trigger: when testing
---
Body
""",
            encoding="utf-8",
        )

        instinct = _parse_instinct_yaml(file_path, "global", None, "user")
        assert instinct is not None
        assert instinct.instinct_id == "fallback"

    def test_import_instincts_logs_parse_errors(self, tmp_path, monkeypatch):
        instincts_dir = tmp_path / "instincts"
        instincts_dir.mkdir()
        yaml_file = instincts_dir / "broken.yaml"
        yaml_file.write_text("trigger: broken\n", encoding="utf-8")
        yml_file = instincts_dir / "broken.yml"
        yml_file.write_text("trigger: broken\n", encoding="utf-8")
        db = MagicMock()

        original_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
            if self in {yaml_file, yml_file}:
                raise OSError("boom")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        assert _import_instincts_from_dir(db, instincts_dir, "project", "proj-1", "user") == 0
        assert db.upsert_instinct.call_count == 0

    def test_project_filter_skips_global_dirs(self, tmp_path):
        from devgear.mem.importers import import_instincts

        db = MagicMock()
        global_dir = tmp_path / ".devgear" / "instincts" / "personal"
        project_dir = tmp_path / ".devgear" / "projects" / "proj-1" / "instincts" / "personal"
        other_project_dir = tmp_path / ".devgear" / "projects" / "proj-2" / "instincts" / "personal"
        global_dir.mkdir(parents=True)
        project_dir.mkdir(parents=True)
        other_project_dir.mkdir(parents=True)
        (tmp_path / ".devgear" / "projects" / "skip.txt").write_text("ignore", encoding="utf-8")
        (global_dir / "global.yaml").write_text("id: global\nconfidence: 0.6\n", encoding="utf-8")
        (project_dir / "project.yaml").write_text("id: project\nconfidence: 0.9\n", encoding="utf-8")
        (other_project_dir / "other.yaml").write_text("id: other\nconfidence: 0.1\n", encoding="utf-8")

        with patch("devgear.mem.importers.DEVGEAR_DIR", tmp_path / ".devgear"):
            result = import_instincts(db, "test_user", project_id="proj-1")

        assert result == 1
        assert db.upsert_instinct.call_count == 1

    def test_duplicate_project_dirs_are_skipped(self, tmp_path):
        from devgear.mem.importers import import_instincts

        db = MagicMock()
        devgear_dir = tmp_path / ".devgear"
        first = devgear_dir / "projects" / "proj-1" / "instincts" / "personal"
        first.mkdir(parents=True)
        (first / "first.yaml").write_text("id: first\nconfidence: 0.6\n", encoding="utf-8")

        with patch("devgear.mem.importers.DEVGEAR_DIR", devgear_dir):
            result = import_instincts(db, "test_user")

        assert result == 1
        assert db.upsert_instinct.call_count == 1

    def test_same_project_in_multiple_dirs_is_skipped(self, tmp_path):
        """異なる projects_dir に同名 project が存在する場合、2 回目はスキップされる（line 64）。"""
        from devgear.mem.importers import import_instincts

        db = MagicMock()
        dir_a = tmp_path / "a" / "projects"
        dir_b = tmp_path / "b" / "projects"
        for base in (dir_a, dir_b):
            personal = base / "proj-1" / "instincts" / "personal"
            personal.mkdir(parents=True)
            (personal / "i.yaml").write_text(
                "id: dup-id\ntrigger: t\nconfidence: 0.9\n",
                encoding="utf-8",
            )

        with patch("devgear.mem.importers._project_dirs", lambda: [dir_a, dir_b]):
            result = import_instincts(db, "test_user")

        # dup-id は dir_a のみで取り込まれ、dir_b はスキップ → 1 回だけ
        assert result == 1
        assert db.upsert_instinct.call_count == 1

    def test_event_logs_same_project_in_multiple_dirs_is_skipped(self, tmp_path):
        """import_event_logs でも 2 回目の同名 project はスキップされる（line 273）。"""
        from devgear.mem.importers import import_event_logs

        db = MagicMock()
        db.exists_event_log_by_natural_key.return_value = False
        dir_a = tmp_path / "a" / "projects"
        dir_b = tmp_path / "b" / "projects"
        for base in (dir_a, dir_b):
            proj = base / "proj-1"
            proj.mkdir(parents=True)
            (proj / "observations.jsonl").write_text(
                json.dumps({"event": "x", "timestamp": 1, "data": {"a": 1}}) + "\n",
                encoding="utf-8",
            )

        with patch("devgear.mem.importers._project_dirs", lambda: [dir_a, dir_b]):
            with patch("devgear.mem.importers.DEVGEAR_DIR", tmp_path / ".devgear"):
                result = import_event_logs(db, "test_user", project_id="proj-1")

        # 重複スキップで dir_a 分のみ取り込み → 1 イベント
        assert result == 1


class TestImportAdrs:
    """import_adrs のテスト"""

    def test_no_adr_dir_returns_zero(self, tmp_path):
        from devgear.mem.importers import import_adrs

        db = MagicMock()
        result = import_adrs(db, "test_user", str(tmp_path))
        assert result == 0

    def test_parses_adr_files(self, tmp_path):
        from devgear.mem.importers import import_adrs

        db = MagicMock()
        db.get_adr_by_key.return_value = None  # 既存データなし

        # ADR ディレクトリを作成
        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)

        # ADR ファイルを作成
        adr_file = adr_dir / "0001-use-postgresql.md"
        adr_file.write_text(
            """# 1. Use PostgreSQL

Date: 2024-01-01

## Status

Accepted

## Context

We need a database.

## Decision

Use PostgreSQL.

## Consequences

Works well.
"""
        )

        result = import_adrs(db, "test_user", str(tmp_path))

        assert result == 1
        db.upsert_adr.assert_called_once()

    def test_extracts_adr_number_from_filename(self, tmp_path):
        from devgear.mem.importers import import_adrs

        db = MagicMock()
        db.get_adr_by_key.return_value = None

        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)

        # 異なる番号のファイルを作成
        (adr_dir / "0042-another-decision.md").write_text("# 42. Another Decision\n\n## Status\n\nProposed")

        result = import_adrs(db, "test_user", str(tmp_path))

        assert result == 1
        call_args = db.upsert_adr.call_args
        adr = call_args[0][0]
        assert adr.adr_number == 42

    def test_parses_title_status_and_fallback_identifier(self, tmp_path, monkeypatch):
        adr_dir = tmp_path / "docs" / "adr"
        adr_dir.mkdir(parents=True)
        adr_file = adr_dir / "0007-introduce-cache.md"
        adr_file.write_text(
            """# ADR-7: Introduce Cache

**Status**: proposed
""",
            encoding="utf-8",
        )
        fallback = adr_dir / "0008-cache.md"
        fallback.write_text("No heading here\n", encoding="utf-8")

        adr = _parse_adr_markdown(adr_file, "project-id", "user")
        assert adr is not None
        assert adr.adr_number == 7
        assert adr.title == "Introduce Cache"
        assert adr.status == "proposed"

        fallback_adr = _parse_adr_markdown(fallback, "project-id", "user")
        assert fallback_adr is not None
        assert fallback_adr.title == "0008-cache"

        monkeypatch.chdir(tmp_path)
        db = MagicMock()
        root = tmp_path
        (root / ".git").mkdir()
        (root / ".git" / "config").write_text("[remote \"origin\"]\n\turl = https://example.com/repo.git\n", encoding="utf-8")
        (root / "docs" / "adr").mkdir(parents=True, exist_ok=True)
        (root / "docs" / "adr" / "README.md").write_text("ignored", encoding="utf-8")
        (root / "docs" / "adr" / "template.md").write_text("ignored", encoding="utf-8")
        (root / "docs" / "adr" / "0009-broken.md").write_text("# Broken\n", encoding="utf-8")

        original_read_text = Path.read_text

        def fake_read_text(self: Path, *args, **kwargs):  # noqa: ANN001
            if self.name == "0009-broken.md":
                raise OSError("boom")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fake_read_text)

        with patch("devgear.mem.importers.Path.read_text", fake_read_text):
            result = import_adrs(db, "user")

        assert result == 2
        assert db.upsert_adr.call_count == 2
        assert _get_project_identifier(root) != root.name

        broken_root = tmp_path / "broken"
        broken_root.mkdir()
        (broken_root / ".git").mkdir()
        (broken_root / ".git" / "config").write_text("[remote \"origin\"]\n\turl = https://example.com/repo.git\n", encoding="utf-8")
        with patch("devgear.mem.importers.Path.read_text", side_effect=OSError("boom")):
            assert _get_project_identifier(broken_root) == "broken"

    def test_skips_files_without_numeric_prefix(self, tmp_path):
        adr_file = tmp_path / "docs" / "adr" / "no-number.md"
        adr_file.parent.mkdir(parents=True)
        adr_file.write_text("# Title\n", encoding="utf-8")

        assert _parse_adr_markdown(adr_file, "project-id", "user") is None


class TestImportEventLogs:
    """import_event_logs のテスト"""

    def test_no_log_files_returns_zero(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()

        # 存在しないディレクトリをモック
        with (
            patch("devgear.mem.importers.DEVGEAR_DIR", tmp_path / "nonexistent" / ".devgear"),
            patch("devgear.mem.importers.DEVGEAR_STATE_DIR", tmp_path / "nonexistent" / "state"),
        ):
            result = import_event_logs(db, "test_user")
        assert result == 0

    def test_parses_jsonl_files(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()

        # observations.jsonl を作成
        devgear_dir = tmp_path / ".devgear"
        devgear_dir.mkdir(parents=True)
        obs_file = devgear_dir / "observations.jsonl"
        obs_file.write_text(
            '{"action": "edit", "file": "test.py", "timestamp": 1704067200}\n'
            '{"action": "read", "file": "main.py", "timestamp": 1704067300}\n'
        )

        state_dir = tmp_path / ".claude" / "state"
        state_dir.mkdir(parents=True)

        with (
            patch("devgear.mem.importers.DEVGEAR_DIR", devgear_dir),
            patch("devgear.mem.importers.DEVGEAR_STATE_DIR", state_dir),
        ):
            result = import_event_logs(db, "test_user")

        assert result == 2
        assert db.store_event_log.call_count == 2

    def test_jsonl_parser_handles_multiple_timestamp_shapes(self, tmp_path, monkeypatch):
        db = MagicMock()
        event_file = tmp_path / "events.jsonl"
        event_file.write_text(
            "\n".join(
                [
                    json.dumps({"timestamp": "2024-01-01T00:00:00Z", "payload": 1}),
                    "",
                    json.dumps({"ts": 1704067300, "payload": 2}),
                    "not-json",
                    json.dumps({"created_at": "bad", "payload": 3}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        monkeypatch.setattr(time, "time", lambda: 12345)
        count = _import_jsonl_events(db, event_file, "observation", "proj", "user")

        assert count == 3
        assert db.store_event_log.call_count == 3
        first_event = db.store_event_log.call_args_list[0].args[0]
        assert first_event.event_type == "observation"
        assert first_event.id.startswith("observation-1704067200-")
        third_event = db.store_event_log.call_args_list[2].args[0]
        assert third_event.created_at_epoch == 12345

    def test_jsonl_import_open_failure_returns_zero(self, tmp_path, monkeypatch):
        db = MagicMock()
        event_file = tmp_path / "events.jsonl"
        event_file.write_text(json.dumps({"payload": "x"}), encoding="utf-8")

        monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
        assert _import_jsonl_events(db, event_file, "observation", None, "user") == 0

    def test_import_event_logs_covers_global_project_and_secondary_files(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()
        devgear_root = tmp_path / ".devgear"
        state_dir = tmp_path / ".claude" / "state"
        project_dir = devgear_root / "projects" / "proj-1"
        project_dir.mkdir(parents=True)
        state_dir.mkdir(parents=True)
        (devgear_root / "observations.jsonl").parent.mkdir(parents=True, exist_ok=True)
        (devgear_root / "observations.jsonl").write_text(json.dumps({"payload": "global"}) + "\n", encoding="utf-8")
        (project_dir / "observations.jsonl").write_text(json.dumps({"payload": "project"}) + "\n", encoding="utf-8")
        (state_dir / "skill-runs.jsonl").write_text(json.dumps({"payload": "skill"}) + "\n", encoding="utf-8")
        logs_dir = devgear_root / "logs"
        logs_dir.mkdir(parents=True)
        (logs_dir / "costs.jsonl").write_text(json.dumps({"payload": "cost"}) + "\n", encoding="utf-8")

        with (
            patch("devgear.mem.importers.DEVGEAR_DIR", devgear_root),
            patch("devgear.mem.importers.DEVGEAR_STATE_DIR", state_dir),
        ):
            result = import_event_logs(db, "test_user")

        assert result == 4
        assert db.store_event_log.call_count == 4

    def test_import_event_logs_honors_project_filter(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()
        devgear_dir = tmp_path / ".devgear"
        project_dir = devgear_dir / "projects" / "proj-1"
        other_dir = devgear_dir / "projects" / "proj-2"
        project_dir.mkdir(parents=True)
        other_dir.mkdir(parents=True)
        (project_dir / "observations.jsonl").write_text(json.dumps({"payload": "project"}) + "\n", encoding="utf-8")
        (other_dir / "observations.jsonl").write_text(json.dumps({"payload": "other"}) + "\n", encoding="utf-8")

        with patch("devgear.mem.importers.DEVGEAR_DIR", devgear_dir):
            result = import_event_logs(db, "test_user", project_id="proj-1")

        assert result == 1
        assert db.store_event_log.call_count == 1

    def test_import_event_logs_skips_non_directory_entries(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()
        devgear_dir = tmp_path / ".devgear"
        projects_dir = devgear_dir / "projects"
        projects_dir.mkdir(parents=True)
        (projects_dir / "skip.txt").write_text("ignore", encoding="utf-8")
        project_dir = projects_dir / "proj-1"
        project_dir.mkdir()
        (project_dir / "observations.jsonl").write_text(json.dumps({"payload": "project"}) + "\n", encoding="utf-8")

        with patch("devgear.mem.importers.DEVGEAR_DIR", devgear_dir):
            result = import_event_logs(db, "test_user")

        assert result == 1
        assert db.store_event_log.call_count == 1

    def test_duplicate_project_dirs_are_skipped(self, tmp_path):
        from devgear.mem.importers import import_event_logs

        db = MagicMock()
        devgear_dir = tmp_path / ".devgear"
        first = devgear_dir / "projects" / "proj-1"
        first.mkdir(parents=True)
        (first / "observations.jsonl").write_text(json.dumps({"payload": "first"}) + "\n", encoding="utf-8")

        with patch("devgear.mem.importers.DEVGEAR_DIR", devgear_dir):
            result = import_event_logs(db, "test_user")

        assert result == 1
        assert db.store_event_log.call_count == 1


class TestImportAll:
    """import_all のテスト"""

    def test_calls_all_importers(self, tmp_path):
        from devgear.mem.importers import import_all

        db = MagicMock()
        db.get_instinct_by_key.return_value = None
        db.get_adr_by_key.return_value = None

        with (
            patch("devgear.mem.importers.DEVGEAR_DIR", tmp_path / "nonexistent" / ".devgear"),
            patch("devgear.mem.importers.DEVGEAR_STATE_DIR", tmp_path / "nonexistent" / "state"),
        ):
            result = import_all(db, "test_user", str(tmp_path))

        assert "instincts" in result
        assert "adrs" in result
        assert "events" in result

    def test_import_all_aggregates_counts(self, tmp_path):
        from devgear.mem.importers import import_all

        db = MagicMock()
        with (
            patch("devgear.mem.importers.import_instincts", return_value=1),
            patch("devgear.mem.importers.import_adrs", return_value=2),
            patch("devgear.mem.importers.import_event_logs", return_value=3),
        ):
            result = import_all(db, "test_user", str(tmp_path), project_id="proj")

        assert result == {"instincts": 1, "adrs": 2, "events": 3}
