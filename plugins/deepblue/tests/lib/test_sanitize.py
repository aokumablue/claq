"""sanitize_log_value のテスト。"""

from __future__ import annotations

import re

import pytest

from devgear.lib.sanitize import sanitize_log_value


@pytest.mark.parametrize(
    ("value", "max_len", "expected"),
    [
        ("plain", 200, "plain"),
        ("line1\nline2\r\nline3", 200, "line1 line2 line3"),
        ("a\x00b\x07c\t d", 200, "a b c d"),
        ("unicode: 日本語 🐍", 200, "unicode: 日本語 🐍"),
    ],
)
def test_sanitize_log_value_normalizes_text(value: str, max_len: int, expected: str) -> None:
    result = sanitize_log_value(value, max_len=max_len)

    assert result == expected
    assert "\n" not in result
    assert "\r" not in result
    assert not re.search(r"[\x00-\x1f\x7f]", result)


def test_sanitize_log_value_truncates_output() -> None:
    result = sanitize_log_value("x" * 500, max_len=12)

    assert result == "x" * 12


def test_sanitize_log_value_handles_empty_value() -> None:
    assert sanitize_log_value("", max_len=10) == ""
