"""hook_common のフック出力ヘルパー（SessionStart/UserPromptSubmit/PostToolUse）と
stdin 読み取りガード（TTY/タイムアウト/バイト上限）のテスト。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from bluecore.hooks.hook_common import (
    detach_process,
    emit_post_tool_use_output,
    emit_session_start_output,
    emit_user_prompt_submit_output,
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


class TestEmitUserPromptSubmitOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "<mem-context>関連メモリ</mem-context>",
        ],
        ids=["simple", "newlines", "unicode", "mem-context"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        payload = json.loads(emit_user_prompt_submit_output(additional_context))
        inner = payload["hookSpecificOutput"]
        assert inner["hookEventName"] == "UserPromptSubmit"
        assert inner["additionalContext"] == additional_context

    def test_no_top_level_event_name(self) -> None:
        """トップレベル hookEventName 形式（旧バグ）に戻っていないこと。"""
        payload = json.loads(emit_user_prompt_submit_output("ctx"))
        assert "hookEventName" not in payload
        assert "additionalContext" not in payload

    def test_unicode_not_escaped(self) -> None:
        assert "日本語" in emit_user_prompt_submit_output("日本語")


class TestEmitPostToolUseOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
        ],
        ids=["simple", "newlines", "unicode"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        payload = json.loads(emit_post_tool_use_output(additional_context))
        inner = payload["hookSpecificOutput"]
        assert inner["hookEventName"] == "PostToolUse"
        assert inner["additionalContext"] == additional_context


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


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self, n: int = -1) -> bytes:
        return self._data[:n] if n >= 0 else self._data


class _FakeStdin:
    """isatty() を備えた stdin の代替オブジェクト。"""

    def __init__(self, text: str, *, tty: bool = False) -> None:
        self._tty = tty
        self.buffer = _FakeBuffer(text.encode("utf-8"))
        self.read_called = False

    def isatty(self) -> bool:
        return self._tty

    def read(self, n: int = -1) -> str:
        raise AssertionError("バイト読みでは text read を使わない")


def _patch_select_ready(monkeypatch: pytest.MonkeyPatch, hook_common) -> None:  # noqa: ANN001
    """select を常に ready 扱いへ差し替える（フェイク stdin は実 fd を持たないため）。"""
    monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: (r, [], []))


class TestStdinReady:
    """_stdin_ready の TTY/タイムアウトガードのテスト（旧 launcher._read_stdin 相当）。"""

    def test_tty_returns_false_without_calling_select(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        def fail_select(*args):  # noqa: ANN002
            raise AssertionError("select must not be called for tty stdin")

        monkeypatch.setattr(hook_common.select, "select", fail_select)

        assert hook_common._stdin_ready() is False

    def test_ready_pipe_returns_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        _patch_select_ready(monkeypatch, hook_common)

        assert hook_common._stdin_ready() is True

    def test_timeout_returns_false_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """select タイムアウト時は stderr 警告のうえ False を返す（NG-B1 回帰）。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        assert hook_common._stdin_ready() is False
        assert "リダイレクト漏れ" in capsys.readouterr().err


class TestReadRawStdin:
    """read_raw_stdin のバイト単位制限・stdin ガードのテスト。"""

    def test_limits_by_bytes_not_chars_with_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer 付き stdin はバイト単位で読み取りを制限する。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("あ" * 10))
        _patch_select_ready(monkeypatch, hook_common)

        result = hook_common.read_raw_stdin(max_bytes=10)

        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）
        assert result == "あああ�"

    def test_text_stdin_is_byte_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer を持たない stdin（io.StringIO 等）もバイト換算で切り捨てる。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("あ" * 10))
        _patch_select_ready(monkeypatch, hook_common)

        result = hook_common.read_raw_stdin(max_bytes=10)

        assert len(result.encode("utf-8")) <= 12  # 置換文字を含む 10 バイト相当
        assert result.startswith("あああ")

    def test_small_input_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """制限未満の入力はそのまま返る。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("hello"))
        _patch_select_ready(monkeypatch, hook_common)

        assert hook_common.read_raw_stdin() == "hello"

    def test_tty_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        assert hook_common.read_raw_stdin() == ""

    def test_select_timeout_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        fake_stdin = _FakeStdin("payload")
        monkeypatch.setattr(hook_common.sys, "stdin", fake_stdin)
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        assert hook_common.read_raw_stdin() == ""
        assert fake_stdin.read_called is False


class TestReadRawStdinWithTruncation:
    """read_raw_stdin_with_truncation の切り捨て判定・stdin ガードのテスト。"""

    def test_no_truncation_when_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("short"))
        _patch_select_ready(monkeypatch, hook_common)

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == "short"
        assert truncated is False

    def test_truncates_when_exceeding_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("a" * 20))
        _patch_select_ready(monkeypatch, hook_common)

        text, truncated = hook_common.read_raw_stdin_with_truncation(max_bytes=10)

        assert text == "a" * 10
        assert truncated is True

    def test_tty_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False

    def test_select_timeout_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))
        monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: ([], [], []))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False


class TestDetachProcess:
    """detach_process の一時ファイル経由 stdin 引き渡し・エラー処理のテスト。

    launcher --bg（非 Claude ハーネス）と session_end の Codex フォール
    バックの双方から呼ばれる共通の detach 実装。
    """

    def test_launches_detached_process_and_cleans_up_tmp_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.Path, "home", lambda: tmp_path)
        captured = {}
        written_stdin_content = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["cmd"] = cmd
                captured["env"] = env
                captured["start_new_session"] = start_new_session
                stdin.seek(0)
                written_stdin_content["text"] = stdin.read()

        monkeypatch.setattr(hook_common.subprocess, "Popen", _FakePopen)

        result = detach_process(["python3", "-m", "bluecore.mem.cli", "observe"], "raw-payload", env={"X": "1"})

        assert result is True
        assert captured["cmd"][:3] == ["timeout", "--kill-after=30", "590"]
        assert captured["cmd"][3:] == ["python3", "-m", "bluecore.mem.cli", "observe"]
        assert captured["env"] == {"X": "1"}
        assert captured["start_new_session"] is True
        assert written_stdin_content["text"] == "raw-payload"
        # 起動直後に unlink 済みで、ディレクトリにファイルが残らないこと。
        assert list((tmp_path / ".bluecore").glob("*.stdin")) == []

    def test_tempfile_creation_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.Path, "home", lambda: tmp_path)

        def fail_named_temp_file(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

        monkeypatch.setattr(hook_common.tempfile, "NamedTemporaryFile", fail_named_temp_file)

        assert detach_process(["true"], "raw") is False

    def test_popen_failure_returns_false_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.Path, "home", lambda: tmp_path)

        def fail_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("spawn failed")

        monkeypatch.setattr(hook_common.subprocess, "Popen", fail_popen)

        assert detach_process(["true"], "raw") is False
        assert list((tmp_path / ".bluecore").glob("*.stdin")) == []

    def test_unlink_failure_during_cleanup_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一時ファイルの unlink 失敗（既に削除済み等）でも起動成功を維持する。"""
        from bluecore.hooks import hook_common

        monkeypatch.setattr(hook_common.Path, "home", lambda: tmp_path)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)

        def fail_unlink(path):  # noqa: ANN001
            raise OSError("already removed")

        monkeypatch.setattr(hook_common.os, "unlink", fail_unlink)

        assert detach_process(["true"], "raw") is True
