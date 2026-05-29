"""skill_evolution.compat のテスト。"""

from __future__ import annotations

from datetime import UTC, datetime

from devgear.lib.skill_evolution import skill_evolution_compat as compat


def test_merge_options_prefers_kwargs_and_does_not_mutate_input() -> None:
    options = {"a": 1, "b": 2}

    merged = compat.merge_options(options, b=3, c=4)

    assert merged == {"a": 1, "b": 3, "c": 4}
    assert options == {"a": 1, "b": 2}


def test_get_option_skips_none_and_empty_string() -> None:
    options = {"first": None, "second": "", "third": 0}

    assert compat.get_option(options, "first", "second", "third") == 0
    assert compat.get_option(options, "missing", default="fallback") == "fallback"


def test_get_value_supports_mapping_and_object() -> None:
    class Sample:
        def __init__(self) -> None:
            self.primary = None
            self.secondary = "value"

    class EmptySample:
        def __init__(self) -> None:
            self.primary = None
            self.secondary = None

    assert compat.get_value({"primary": None, "secondary": "mapping"}, "primary", "secondary") == "mapping"
    assert compat.get_value(Sample(), "primary", "secondary") == "value"
    assert compat.get_value(EmptySample(), "primary", "secondary", default="fallback") == "fallback"
    assert compat.get_value(None, "secondary", default="fallback") == "fallback"


def test_parse_iso_timestamp_and_is_iso_timestamp_handle_edge_cases() -> None:
    parsed = compat.parse_iso_timestamp(" 2026-03-15T12:00:00Z ")

    assert parsed == datetime(2026, 3, 15, 12, 0, tzinfo=UTC)
    assert compat.parse_iso_timestamp("") is None
    assert compat.parse_iso_timestamp(123) is None
    assert compat.parse_iso_timestamp("not-a-timestamp") is None
    assert compat.is_iso_timestamp("2026-03-15T12:00:00Z") is True
    assert compat.is_iso_timestamp("bad") is False


def test_to_iso_string_normalizes_timezone() -> None:
    naive = datetime(2026, 3, 15, 12, 0, 0)
    aware = datetime(2026, 3, 15, 21, 0, 0, tzinfo=UTC)

    assert compat.to_iso_string(naive) == "2026-03-15T12:00:00Z"
    assert compat.to_iso_string(aware) == "2026-03-15T21:00:00Z"


def test_utc_now_iso_uses_z_suffix(monkeypatch) -> None:
    class FakeDatetime:
        @classmethod
        def now(cls, tz):  # noqa: ANN001
            assert tz is UTC
            return datetime(2026, 3, 15, 12, 34, 56, 123000, tzinfo=UTC)

    monkeypatch.setattr(compat, "datetime", FakeDatetime)

    assert compat.utc_now_iso() == "2026-03-15T12:34:56.123000Z"
