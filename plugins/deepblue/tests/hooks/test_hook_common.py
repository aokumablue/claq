"""hook_common の SessionStart 出力ヘルパーのテスト。"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from devgear.hooks.hook_common import (
    SESSION_START_HOOK_IDS,
    emit_session_start_output,
    print_session_start_output,
)


def _parse_session_start(output: str) -> dict:
    """出力が有効な SessionStart hookSpecificOutput JSON かを検証して返す。"""
    payload = json.loads(output)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert "additionalContext" in inner
    return inner


class TestEmitSessionStartOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "",
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "a" * 5000,
        ],
        ids=["empty", "simple", "newlines", "unicode", "long"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        result = emit_session_start_output(additional_context)
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == additional_context

    def test_default_empty_context(self) -> None:
        result = emit_session_start_output()
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == ""

    def test_is_string(self) -> None:
        assert isinstance(emit_session_start_output(), str)

    def test_no_trailing_newline(self) -> None:
        result = emit_session_start_output()
        assert not result.endswith("\n")


class TestPrintSessionStartOutput:
    def test_prints_to_stdout(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output("hello")
        output = buf.getvalue()
        inner = _parse_session_start(output.strip())
        assert inner["additionalContext"] == "hello"

    def test_default_empty_context(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output()
        inner = _parse_session_start(buf.getvalue().strip())
        assert inner["additionalContext"] == ""


class TestSessionStartHookIds:
    def test_is_frozenset(self) -> None:
        assert isinstance(SESSION_START_HOOK_IDS, frozenset)

    def test_contains_required_ids(self) -> None:
        required = {
            "session:start",
            "session:mem:setup",
            "session:mem:context",
            "session:mem:record-project-profile",
        }
        assert required.issubset(SESSION_START_HOOK_IDS)
