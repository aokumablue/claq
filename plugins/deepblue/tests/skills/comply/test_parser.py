"""parser モジュールのテスト — JSONLトレースとYAML仕様の解析。"""

from pathlib import Path

import pytest

from devgear.skills.comply.parser import (
    ComplianceSpec,
    Detector,
    ObservationEvent,
    Step,
    parse_spec,
    parse_trace,
)

FIXTURES = (
    Path(__file__).resolve().parents[3] / "src" / "devgear" / "skills" / "comply" / "fixtures"
)


class TestParseTrace:
    def test_parses_compliant_trace(self) -> None:
        events = parse_trace(FIXTURES / "compliant_trace.jsonl")
        assert len(events) == 5
        assert all(isinstance(e, ObservationEvent) for e in events)

    def test_events_sorted_by_timestamp(self) -> None:
        events = parse_trace(FIXTURES / "compliant_trace.jsonl")
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)

    def test_event_fields(self) -> None:
        events = parse_trace(FIXTURES / "compliant_trace.jsonl")
        first = events[0]
        assert first.tool == "Write"
        assert first.session == "sess-001"
        assert "test_fib.py" in first.input
        assert first.output == "File created"

    def test_parses_noncompliant_trace(self) -> None:
        events = parse_trace(FIXTURES / "noncompliant_trace.jsonl")
        assert len(events) == 3
        assert "src/fib.py" in events[0].input

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.jsonl"
        empty.write_text("")
        events = parse_trace(empty)
        assert events == []

    def test_nonexistent_file_raises(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_trace(Path("/nonexistent/trace.jsonl"))


class TestParseSpec:
    def test_parses_tdd_spec(self) -> None:
        spec = parse_spec(FIXTURES / "tdd_spec.yaml")
        assert isinstance(spec, ComplianceSpec)
        assert spec.id == "tdd-workflow"
        assert len(spec.steps) == 5

    def test_step_fields(self) -> None:
        spec = parse_spec(FIXTURES / "tdd_spec.yaml")
        first = spec.steps[0]
        assert isinstance(first, Step)
        assert first.id == "write_test"
        assert first.required is True
        assert isinstance(first.detector, Detector)
        assert "test file" in first.detector.description
        assert first.detector.before_step == "write_impl"

    def test_optional_detector_fields(self) -> None:
        spec = parse_spec(FIXTURES / "tdd_spec.yaml")
        write_test = spec.steps[0]
        assert write_test.detector.after_step is None

        run_test_red = spec.steps[1]
        assert run_test_red.detector.after_step == "write_test"
        assert run_test_red.detector.before_step == "write_impl"

    def test_scoring_threshold(self) -> None:
        spec = parse_spec(FIXTURES / "tdd_spec.yaml")
        assert spec.threshold_promote_to_hook == 0.6

    def test_required_vs_optional_steps(self) -> None:
        spec = parse_spec(FIXTURES / "tdd_spec.yaml")
        required = [s for s in spec.steps if s.required]
        optional = [s for s in spec.steps if not s.required]
        assert len(required) == 4
        assert len(optional) == 1
        assert optional[0].id == "refactor"

    def test_nonexistent_spec_raises_file_not_found(self) -> None:
        with pytest.raises(FileNotFoundError):
            parse_spec(Path("/nonexistent/spec.yaml"))

    def test_spec_without_scoring_raises_key_error(self, tmp_path: Path) -> None:
        spec_yaml = tmp_path / "no_scoring.yaml"
        spec_yaml.write_text(
            "id: test\nname: Test\nsource_rule: rule\nversion: '1.0'\n"
            "steps:\n  - id: s1\n    description: desc\n    required: true\n"
            "    detector:\n      description: det\n",
            encoding="utf-8",
        )
        with pytest.raises(KeyError, match="scoring"):
            parse_spec(spec_yaml)


class TestParseTraceErrors:
    """parse_trace のエラーパステスト"""

    def test_invalid_json_line_raises_value_error(self, tmp_path: Path) -> None:
        import json

        trace = tmp_path / "bad.jsonl"
        # 1行目: 有効 JSON、2行目: 不正 JSON
        valid_line = json.dumps(
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "event": "tool_use",
                "tool": "Write",
                "session": "sess-001",
            }
        )
        trace.write_text(valid_line + "\n{invalid json\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Invalid JSON at line 2"):
            parse_trace(trace)

    def test_missing_required_field_raises_value_error(self, tmp_path: Path) -> None:
        import json

        trace = tmp_path / "missing_field.jsonl"
        # timestamp フィールドを欠落させる
        line = json.dumps(
            {
                "event": "tool_use",
                "tool": "Write",
                "session": "sess-001",
            }
        )
        trace.write_text(line + "\n", encoding="utf-8")
        with pytest.raises(ValueError, match="Missing required field"):
            parse_trace(trace)

    def test_optional_fields_default_to_empty_string(self, tmp_path: Path) -> None:
        import json

        trace = tmp_path / "no_optional.jsonl"
        line = json.dumps(
            {
                "timestamp": "2024-01-01T00:00:00Z",
                "event": "tool_use",
                "tool": "Read",
                "session": "sess-001",
                # input, output は省略
            }
        )
        trace.write_text(line + "\n", encoding="utf-8")
        events = parse_trace(trace)
        assert events[0].input == ""
        assert events[0].output == ""
