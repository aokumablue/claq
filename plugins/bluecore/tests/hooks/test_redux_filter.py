"""redux_filter フック（PostToolUse）のテスト。"""

from __future__ import annotations

import json
import re

import pytest

from bluecore.hooks import redux_filter as hook
from bluecore.mem.settings import ReduxSettings, Settings
from bluecore.redux.config import ReduxConfig
from bluecore.redux.engine import ReduxEngine, ReduxFilterSpec


def _engine(limit: int = 1) -> ReduxEngine:
    """全コマンドにマッチし limit_lines で圧縮するテスト用エンジン。"""
    spec = ReduxFilterSpec(name="t", command_pattern=re.compile(".*"), limit_lines=limit)
    return ReduxEngine([spec])


def _payload(tool_name: str = "Bash", stdout: str = "", command: str = "ps aux", **response_extra: object) -> str:
    """テスト用ペイロード JSON を生成する（tool_response はツール出力オブジェクト）。"""
    tool_response: dict[str, object] = {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False}
    tool_response.update(response_extra)
    return json.dumps({"tool_name": tool_name, "tool_input": {"command": command}, "tool_response": tool_response})


def _updated(result: str) -> dict:
    """updatedToolOutput オブジェクトを取り出す。"""
    return json.loads(result)["hookSpecificOutput"]["updatedToolOutput"]


class _BoomEngine:
    """reduce が必ず例外を投げるエンジン（フォールバック検証用）。"""

    def reduce(self, command: str, output: str, config: ReduxConfig) -> str:
        raise RuntimeError("boom")


class _CaptureEngine:
    """reduce に渡された command を記録するエンジン（引数検証用）。"""

    def __init__(self) -> None:
        self.command = ""

    def reduce(self, command: str, output: str, config: ReduxConfig) -> str:
        self.command = command
        return "x"


class TestEvaluate:
    def test_invalid_json_returns_empty(self) -> None:
        assert hook.evaluate("not json", config=ReduxConfig(), engine=_engine()) == ""

    def test_non_bash_returns_empty(self) -> None:
        payload = _payload(tool_name="Read", stdout="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == ""

    def test_non_dict_tool_response_returns_empty(self) -> None:
        # tool_response が文字列（ツール出力 shape でない）→ 透過
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ps"}, "tool_response": "a\nb\nc"})
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == ""

    def test_no_stdout_returns_empty(self) -> None:
        payload = _payload(stdout="")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == ""

    def test_non_str_stdout_returns_empty(self) -> None:
        payload = _payload(stdout="")
        payload = payload.replace('"stdout": ""', '"stdout": 123')
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == ""

    def test_blank_stdout_returns_empty(self) -> None:
        payload = _payload(stdout="   ")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == ""

    def test_disabled_returns_empty(self) -> None:
        payload = _payload(stdout="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(enabled=False), engine=_engine()) == ""

    def test_reduces_output_updates_tool_output(self) -> None:
        body = "\n".join(f"data line {i}" for i in range(50))
        payload = _payload(stdout=body, exitCode=0)
        result = hook.evaluate(payload, config=ReduxConfig(), engine=_engine(limit=1))
        out = json.loads(result)
        assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
        updated = out["hookSpecificOutput"]["updatedToolOutput"]
        assert updated["stdout"] == "data line 0\n... (49 行切り捨て)"
        assert len(updated["stdout"]) < len(body)
        # stdout 以外のツール出力キーは保持される（output shape を維持）
        assert updated["stderr"] == ""
        assert updated["interrupted"] is False
        assert updated["isImage"] is False
        assert updated["exitCode"] == 0

    def test_no_effect_returns_empty(self) -> None:
        # 1 行なので limit_lines=1 では圧縮されない
        payload = _payload(stdout="single line")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine(limit=1)) == ""

    def test_reduction_exception_returns_empty(self) -> None:
        payload = _payload(stdout="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_BoomEngine()) == ""  # type: ignore[arg-type]

    def test_long_command_is_truncated(self) -> None:
        engine = _CaptureEngine()
        payload = _payload(stdout="a\nb\nc", command="echo " + "z" * 5000)
        hook.evaluate(payload, config=ReduxConfig(), engine=engine)  # type: ignore[arg-type]
        assert len(engine.command) == hook._MAX_COMMAND_LEN

    def test_config_none_loads_settings(self) -> None:
        body = "\n".join(f"row {i}" for i in range(50))
        payload = _payload(stdout=body)
        result = hook.evaluate(payload, engine=_engine(limit=1))
        assert _updated(result)["stdout"] == "row 0\n... (49 行切り捨て)"

    def test_engine_none_uses_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_ENGINE", None)
        payload = _payload(stdout="x", command="echo x")
        # 実エンジンで圧縮効果なし → 空文字列（例外なく通ること）
        assert hook.evaluate(payload, config=ReduxConfig()) == ""


class TestLoadConfig:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings()
        settings.redux = ReduxSettings(enabled=False, max_output_len=123)
        monkeypatch.setattr(hook, "Settings", lambda: settings)
        cfg = hook._load_config()
        assert cfg.enabled is False
        assert cfg.max_output_len == 123

    def test_failure_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom() -> Settings:
            raise RuntimeError("settings failed")

        monkeypatch.setattr(hook, "Settings", _boom)
        cfg = hook._load_config()
        assert cfg == ReduxConfig()


class TestToReduxConfig:
    def test_maps_all_fields(self) -> None:
        rs = ReduxSettings(
            enabled=False,
            smart_filter_enabled=False,
            group_lint_enabled=False,
            dedup_enabled=False,
            smart_truncate_enabled=False,
            max_output_len=10,
            head_lines=2,
            tail_lines=3,
            dedup_threshold=4,
        )
        cfg = hook._to_redux_config(rs)
        assert cfg.enabled is False
        assert cfg.smart_filter_enabled is False
        assert cfg.max_output_len == 10
        assert cfg.head_lines == 2
        assert cfg.tail_lines == 3
        assert cfg.dedup_threshold == 4


class TestGetEngine:
    def test_caches_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_ENGINE", None)
        first = hook._get_engine()
        second = hook._get_engine()
        assert first is second


class TestMain:
    def test_success_writes_output(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _payload(stdout="a\nb\nc\nd")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(hook, "evaluate", lambda raw: '{"hookSpecificOutput": {}}')
        assert hook.main() == 0
        assert capsys.readouterr().out == '{"hookSpecificOutput": {}}'

    def test_empty_output_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _payload(stdout="a")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(hook, "evaluate", lambda raw: "")
        assert hook.main() == 0
        assert capsys.readouterr().out == ""

    def test_exception_writes_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _payload(stdout="a\nb")

        def _boom(raw: str) -> str:
            raise RuntimeError("eval failed")

        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(hook, "evaluate", _boom)
        assert hook.main() == 0
        assert capsys.readouterr().out == ""


class TestCopilotOutputContract:
    """Copilot 環境での modifiedResult 契約のテスト。"""

    @pytest.fixture(autouse=True)
    def _reset_harness(self, monkeypatch):
        """ハーネス判定キャッシュをリセットする。"""
        from bluecore.lib import harness

        monkeypatch.delenv("CLAUDECODE", raising=False)
        harness.detect_harness.cache_clear()
        yield
        harness.detect_harness.cache_clear()

    def test_copilot_emits_modified_result(self, monkeypatch):
        """Copilot では modifiedResult 契約で圧縮出力を返す。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        config = ReduxConfig(enabled=True, max_output_len=10, head_lines=1, tail_lines=1)
        raw = _payload(stdout="line1\n" * 100)
        result = hook.evaluate(raw, config=config, engine=ReduxEngine.load())
        payload = json.loads(result)
        assert "hookSpecificOutput" not in payload
        assert payload["modifiedResult"]["resultType"] == "success"
        assert payload["modifiedResult"]["textResultForLlm"]
        assert len(payload["modifiedResult"]["textResultForLlm"]) < len("line1\n" * 100)

    def test_codex_keeps_claude_contract(self, monkeypatch):
        """Codex では Claude 形式（updatedToolOutput）のまま出力する。"""
        monkeypatch.setenv("PLUGIN_DATA", "/tmp/data")
        config = ReduxConfig(enabled=True, max_output_len=10, head_lines=1, tail_lines=1)
        raw = _payload(stdout="line1\n" * 100)
        result = hook.evaluate(raw, config=config, engine=ReduxEngine.load())
        assert "updatedToolOutput" in json.loads(result)["hookSpecificOutput"]
