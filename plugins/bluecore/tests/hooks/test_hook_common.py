"""hook_common の SessionStart 出力ヘルパーのテスト。"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from bluecore.hooks.hook_common import (
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


class TestReadRawStdin:
    """read_raw_stdin のバイト単位制限のテスト。"""

    def test_limits_by_bytes_not_chars_with_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer 付き stdin はバイト単位で読み取りを制限する。"""
        from bluecore.hooks import hook_common

        class _FakeBuffer:
            def __init__(self, data: bytes) -> None:
                self._data = data

            def read(self, n: int = -1) -> bytes:
                return self._data[:n] if n >= 0 else self._data

        class _FakeStdin:
            def __init__(self, text: str) -> None:
                self.buffer = _FakeBuffer(text.encode("utf-8"))

            def read(self, n: int = -1) -> str:
                raise AssertionError("バイト読みでは text read を使わない")

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("あ" * 10))

        result = hook_common.read_raw_stdin(max_bytes=10)

        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）
        assert result == "あああ�"

    def test_text_stdin_is_byte_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer を持たない stdin（io.StringIO 等）もバイト換算で切り捨てる。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("あ" * 10))

        result = hook_common.read_raw_stdin(max_bytes=10)

        assert len(result.encode("utf-8")) <= 12  # 置換文字を含む 10 バイト相当
        assert result.startswith("あああ")

    def test_small_input_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """制限未満の入力はそのまま返る。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("hello"))

        assert hook_common.read_raw_stdin() == "hello"
