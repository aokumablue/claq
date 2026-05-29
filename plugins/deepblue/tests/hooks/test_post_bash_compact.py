"""post_bash_compact フックのテスト。"""

from __future__ import annotations

import json

import pytest

from devgear.hooks import post_bash_compact as hook
from devgear.mem.reducer import ReduceConfig
from devgear.mem.settings import CompactSettings


def _make_payload(
    tool_name: str = "Bash",
    command: str = "ruff check src",
    tool_response: str = "output",
) -> str:
    """テスト用の JSON ペイロードを生成する。"""
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "tool_response": tool_response,
        }
    )


class TestEvaluate:
    """evaluate() のテスト"""

    def test_non_bash_tool_passthrough(self) -> None:
        payload = _make_payload(tool_name="Read")
        assert hook.evaluate(payload, config=ReduceConfig()) == payload

    def test_empty_tool_response_passthrough(self) -> None:
        payload = _make_payload(tool_response="")
        assert hook.evaluate(payload, config=ReduceConfig()) == payload

    def test_whitespace_only_response_passthrough(self) -> None:
        payload = _make_payload(tool_response="   \n  ")
        assert hook.evaluate(payload, config=ReduceConfig()) == payload

    def test_invalid_json_passthrough(self) -> None:
        assert hook.evaluate("not json", config=ReduceConfig()) == "not json"

    def test_reduces_large_output(self) -> None:
        # 大量の ruff エラーで削減効果が出るケース
        ruff_lines = "\n".join(f"src/foo.py:{i}:1: E501 line too long (120 > 88 characters)" for i in range(1, 50))
        payload = _make_payload(tool_response=ruff_lines)
        config = ReduceConfig(max_output_len=10000)
        result = hook.evaluate(payload, config=config)
        result_data = json.loads(result)
        assert len(result_data["tool_response"]) < len(ruff_lines), "削減されるべき"
        assert "[E501]" in result_data["tool_response"]

    def test_no_effect_passthrough(self) -> None:
        # 既に短い出力は変更されない
        payload = _make_payload(tool_response="ok")
        config = ReduceConfig()
        assert hook.evaluate(payload, config=config) == payload

    def test_disabled_config_passthrough(self) -> None:
        config = ReduceConfig(enabled=False)
        payload = _make_payload(tool_response="x" * 5000)
        assert hook.evaluate(payload, config=config) == payload

    def test_tool_input_as_string_json(self) -> None:
        """tool_input が JSON 文字列形式でも正常に処理される。"""
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": json.dumps({"command": "ruff check src"}),
                "tool_response": "x" * 5000,
            }
        )
        config = ReduceConfig(max_output_len=100, head_lines=2, tail_lines=2)
        result = hook.evaluate(payload, config=config)
        # 削減が実行されることを確認
        result_data = json.loads(result)
        assert len(result_data["tool_response"]) < 5000

    def test_stderr_reports_reduction_ratio(self, monkeypatch: pytest.MonkeyPatch) -> None:
        messages: list[str] = []
        monkeypatch.setattr(hook, "write_stderr", messages.append)

        ruff_lines = "\n".join(f"src/foo.py:{i}:1: E501 too long" for i in range(1, 50))
        payload = _make_payload(tool_response=ruff_lines)
        config = ReduceConfig(max_output_len=10000)
        hook.evaluate(payload, config=config)

        assert any("削減" in m for m in messages), "削減率が stderr に報告されるべき"

    def test_tool_input_invalid_json_string_fallback(self) -> None:
        """tool_input が不正な JSON 文字列の場合、コマンドを空文字として扱う。"""
        payload = json.dumps(
            {
                "tool_name": "Bash",
                "tool_input": "{invalid json",  # 不正な JSON 文字列
                "tool_response": "x" * 5000,
            }
        )
        config = ReduceConfig(max_output_len=100, head_lines=2, tail_lines=2)
        # 例外を発生させずに削減が実行されることを確認
        result = hook.evaluate(payload, config=config)
        result_data = json.loads(result)
        assert len(result_data["tool_response"]) < 5000

    def test_settings_load_called_when_config_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """config=None 時に Settings.load() が呼ばれ、CompactSettings が ReduceConfig に変換される。"""
        from devgear.mem import settings as settings_mod

        fake_compact = CompactSettings(enabled=True, max_output_len=10000)

        class _FakeSettings:
            compact = fake_compact

        monkeypatch.setattr(settings_mod.Settings, "load", classmethod(lambda cls: _FakeSettings()))

        ruff_lines = "\n".join(f"src/foo.py:{i}:1: E501 line too long" for i in range(1, 50))
        payload = _make_payload(tool_response=ruff_lines)
        result = hook.evaluate(payload, config=None)
        result_data = json.loads(result)
        # 削減が実行されている
        assert len(result_data["tool_response"]) < len(ruff_lines)

    def test_settings_load_error_fallback_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Settings.load() が例外を投げた場合、デフォルト ReduceConfig にフォールバックする。"""
        from devgear.mem import settings as settings_mod

        def _raise(*_args, **_kwargs):
            raise OSError("settings file corrupted")

        monkeypatch.setattr(
            settings_mod.Settings,
            "load",
            classmethod(lambda cls: (_ for _ in ()).throw(OSError("settings file corrupted"))),
        )

        stderr_messages: list[str] = []
        monkeypatch.setattr(hook, "write_stderr", stderr_messages.append)

        ruff_lines = "\n".join(f"src/foo.py:{i}:1: E501 line too long" for i in range(1, 50))
        payload = _make_payload(tool_response=ruff_lines)
        result = hook.evaluate(payload, config=None)
        # エラーが発生しても削減が実行される（デフォルト設定）
        assert json.loads(result)  # 有効な JSON
        assert any("settings load failed" in m for m in stderr_messages)

    def test_reduction_exception_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """reduce_bash_output が例外を投げた場合、raw をそのまま返す。"""

        def _raise(*_args, **_kwargs):
            raise RuntimeError("unexpected reduce error")

        # hook モジュール内の reduce_bash_output 参照をパッチ
        monkeypatch.setattr(hook, "reduce_bash_output", _raise)

        stderr_messages: list[str] = []
        monkeypatch.setattr(hook, "write_stderr", stderr_messages.append)

        payload = _make_payload(tool_response="x" * 5000)
        config = ReduceConfig()
        result = hook.evaluate(payload, config=config)
        # 例外時は元のペイロードを返す
        assert result == payload
        assert any("reduction failed" in m for m in stderr_messages)

    def test_to_reduce_config_maps_all_fields(self) -> None:
        """_to_reduce_config() がすべてのフィールドを正しく ReduceConfig に変換する。"""
        compact = CompactSettings(
            enabled=False,
            smart_filter_enabled=False,
            group_lint_enabled=False,
            dedup_enabled=False,
            smart_truncate_enabled=False,
            max_output_len=999,
            head_lines=5,
            tail_lines=7,
            dedup_threshold=10,
        )
        rc = hook._to_reduce_config(compact)
        assert rc.enabled is False
        assert rc.smart_filter_enabled is False
        assert rc.group_lint_enabled is False
        assert rc.dedup_enabled is False
        assert rc.smart_truncate_enabled is False
        assert rc.max_output_len == 999
        assert rc.head_lines == 5
        assert rc.tail_lines == 7
        assert rc.dedup_threshold == 10


class TestMainAsScript:
    """__main__ ブロックのテスト（スクリプト直接実行）。"""

    def test_script_exits_zero_and_passthrough(self) -> None:
        """__main__ ブロック: スクリプト直接実行で終了コード 0 かつ raw をそのまま返す。"""
        import os
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).parents[4]
        src_path = repo_root / "plugins/devgear/src"
        script = src_path / "devgear/hooks/post_bash_compact.py"
        payload = _make_payload(tool_response="short")
        env = {**os.environ, "PYTHONPATH": str(src_path)}
        result = subprocess.run(
            [sys.executable, str(script)],
            input=payload,
            text=True,
            capture_output=True,
            env=env,
        )
        assert result.returncode == 0, f"stderr: {result.stderr}"
        assert payload in result.stdout


class TestMain:
    """main() エントリポイントのテスト。"""

    def test_main_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() は常に 0 を返す。"""
        payload = _make_payload(tool_response="ok")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        captured: list[str] = []
        monkeypatch.setattr(hook, "write_stdout", captured.append)
        assert hook.main() == 0

    def test_main_outputs_to_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main() は evaluate() の結果を stdout に書き出す。"""
        payload = _make_payload(tool_response="short")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        captured: list[str] = []
        monkeypatch.setattr(hook, "write_stdout", captured.append)
        hook.main()
        assert len(captured) == 1
        assert "short" in captured[0]

    def test_main_passthrough_on_unexpected_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """evaluate() が例外を投げた場合、main() は raw をそのまま stdout に出力する。"""
        payload = _make_payload(tool_response="hello")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)

        def _raise(_: str, **__):
            raise RuntimeError("boom")

        monkeypatch.setattr(hook, "evaluate", _raise)
        stderr_messages: list[str] = []
        monkeypatch.setattr(hook, "write_stderr", stderr_messages.append)
        captured: list[str] = []
        monkeypatch.setattr(hook, "write_stdout", captured.append)

        ret = hook.main()
        assert ret == 0
        assert captured[0] == payload  # raw をそのまま返す
        assert any("unexpected error" in m for m in stderr_messages)
