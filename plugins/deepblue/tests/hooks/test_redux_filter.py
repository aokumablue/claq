"""redux_filter フック（PostToolUse）のテスト。"""

from __future__ import annotations

import json
import re

import pytest

from deepblue.hooks import redux_filter as hook
from deepblue.mem.settings import ReduxSettings, Settings
from deepblue.redux.config import ReduxConfig
from deepblue.redux.engine import ReduxEngine, ReduxFilterSpec


def _engine(limit: int = 1) -> ReduxEngine:
    """全コマンドにマッチし limit_lines で圧縮するテスト用エンジン。"""
    spec = ReduxFilterSpec(name="t", command_pattern=re.compile(".*"), limit_lines=limit)
    return ReduxEngine([spec])


def _payload(tool_name: str = "Bash", tool_response: str = "", command: str = "ps aux") -> str:
    """テスト用ペイロード JSON を生成する。"""
    return json.dumps(
        {
            "tool_name": tool_name,
            "tool_input": {"command": command},
            "tool_response": tool_response,
        }
    )


class _BoomEngine:
    """reduce が必ず例外を投げるエンジン（フォールバック検証用）。"""

    def reduce(self, command: str, output: str, config: ReduxConfig) -> str:
        raise RuntimeError("boom")


class TestEvaluate:
    def test_invalid_json_passthrough(self) -> None:
        assert hook.evaluate("not json", config=ReduxConfig(), engine=_engine()) == "not json"

    def test_non_bash_passthrough(self) -> None:
        payload = _payload(tool_name="Read", tool_response="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == payload

    def test_no_tool_response_passthrough(self) -> None:
        payload = _payload(tool_response="")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == payload

    def test_blank_tool_response_passthrough(self) -> None:
        payload = _payload(tool_response="   ")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine()) == payload

    def test_disabled_passthrough(self) -> None:
        payload = _payload(tool_response="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(enabled=False), engine=_engine()) == payload

    def test_reduces_output(self) -> None:
        body = "\n".join(f"data line {i}" for i in range(50))
        payload = _payload(tool_response=body)
        result = hook.evaluate(payload, config=ReduxConfig(), engine=_engine(limit=1))
        data = json.loads(result)
        assert data["tool_response"] == "data line 0\n... (49 行切り捨て)"
        assert len(data["tool_response"]) < len(body)

    def test_no_effect_passthrough(self) -> None:
        # 1 行なので limit_lines=1 では圧縮されない
        payload = _payload(tool_response="single line")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_engine(limit=1)) == payload

    def test_reduction_exception_passthrough(self) -> None:
        payload = _payload(tool_response="a\nb\nc")
        assert hook.evaluate(payload, config=ReduxConfig(), engine=_BoomEngine()) == payload  # type: ignore[arg-type]

    def test_config_none_loads_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: Settings()))
        body = "\n".join(f"row {i}" for i in range(50))
        payload = _payload(tool_response=body)
        result = hook.evaluate(payload, engine=_engine(limit=1))
        assert json.loads(result)["tool_response"] == "row 0\n... (49 行切り捨て)"

    def test_engine_none_uses_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(hook, "_ENGINE", None)
        payload = _payload(tool_response="x", command="echo x")
        # 実エンジンで圧縮効果なし → raw パススルー（例外なく通ること）
        assert hook.evaluate(payload, config=ReduxConfig()) == payload


class TestLoadConfig:
    def test_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        settings = Settings()
        settings.redux = ReduxSettings(enabled=False, max_output_len=123)
        monkeypatch.setattr(Settings, "load", classmethod(lambda cls: settings))
        cfg = hook._load_config()
        assert cfg.enabled is False
        assert cfg.max_output_len == 123

    def test_failure_returns_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(cls: type[Settings]) -> Settings:
            raise RuntimeError("load failed")

        monkeypatch.setattr(Settings, "load", classmethod(_boom))
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
    def test_success(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        payload = _payload(tool_response="a\nb\nc\nd")
        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(hook, "evaluate", lambda raw: '{"tool_response": "x"}')
        assert hook.main() == 0
        assert capsys.readouterr().out == '{"tool_response": "x"}'

    def test_exception_falls_back_to_raw(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        payload = _payload(tool_response="a\nb")

        def _boom(raw: str) -> str:
            raise RuntimeError("eval failed")

        monkeypatch.setattr(hook, "read_raw_stdin", lambda: payload)
        monkeypatch.setattr(hook, "evaluate", _boom)
        assert hook.main() == 0
        assert capsys.readouterr().out == payload
