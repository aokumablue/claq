"""bluecore.hooks.pre_agent_nudge のテスト。

PreToolUse フック: Task/Agent ツール起動時に general-purpose サブエージェントを
検知して bluecore 専門エージェント対応表を additionalContext で提示する。
"""

from __future__ import annotations

import json
import runpy
from unittest.mock import patch

import pytest

from bluecore.hooks import pre_agent_nudge


def _capture_io(monkeypatch: pytest.MonkeyPatch, payload: str) -> tuple[list[str], list[str]]:
    """stdout / stderr をキャプチャして pre_agent_nudge.main() を呼べる状態にする。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        payload: read_raw_stdin が返す文字列。

    Returns:
        (stdout_lines, stderr_lines) のタプル。
    """
    stdout: list[str] = []
    stderr: list[str] = []
    monkeypatch.setattr(pre_agent_nudge, "read_raw_stdin", lambda: payload)
    if hasattr(pre_agent_nudge, "write_stdout"):
        monkeypatch.setattr(pre_agent_nudge, "write_stdout", stdout.append)
    if hasattr(pre_agent_nudge, "write_stderr"):
        monkeypatch.setattr(pre_agent_nudge, "write_stderr", stderr.append)
    return stdout, stderr


class TestPreAgentNudgeGeneralPurpose:
    """general-purpose subagent_type 検知テスト。"""

    def test_general_purpose_emits_hook_specific_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """general-purpose 検知時に hookSpecificOutput を stdout に書き出す。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "general-purpose", "prompt": "hello"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert len(stdout) == 1
        parsed = json.loads(stdout[0])
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_general_purpose_context_contains_agent_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """general-purpose 検知時に additionalContext に AGENT_TABLE を含む。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        pre_agent_nudge.main()
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bluecore:explorer" in ctx
        assert "bluecore:tdd-writer" in ctx

    def test_task_tool_general_purpose_emits_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Task ツールでも general-purpose なら出力する。"""
        payload = json.dumps({
            "tool_name": "Task",
            "tool_input": {"subagent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert len(stdout) == 1


class TestPreAgentNudgeNoOutput:
    """出力なし・return 0 のパターン。"""

    @pytest.mark.parametrize("subagent_type", [
        "bluecore:explorer",
        "bluecore:tdd-writer",
        "bluecore:reviewer",
        "custom-agent",
    ])
    def test_non_general_purpose_returns_0_no_output(
        self,
        monkeypatch: pytest.MonkeyPatch,
        subagent_type: str,
    ) -> None:
        """general-purpose 以外では出力なし・return 0。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": subagent_type},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []

    def test_missing_subagent_type_returns_0_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """subagent_type 欠落では出力なし・return 0。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"prompt": "hello"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []

    def test_tool_input_not_dict_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool_input が非 dict なら return 0。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": "invalid",
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []

    def test_missing_tool_input_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """tool_input 欠落なら return 0。"""
        payload = json.dumps({"tool_name": "Agent"})
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []

    def test_empty_input_returns_0(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空入力なら return 0。"""
        stdout, _ = _capture_io(monkeypatch, "")
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []

    def test_subagent_type_none_returns_0_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """subagent_type が null なら出力なし・return 0。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": None},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []


class TestPreAgentNudgeCopilot:
    """Copilot 環境下での出力形式テスト（現仮説: Claude 形式と同一）。"""

    def test_copilot_emits_hook_specific_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Copilot 環境でも hookSpecificOutput 形式を返す（第一仮説）。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="copilot"):
            result = pre_agent_nudge.main()
        assert result == 0
        parsed = json.loads(stdout[0])
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"


class TestAgentTableRegression:
    """AGENT_TABLE 退行防止テスト。"""

    @pytest.mark.parametrize("agent_name", [
        "bluecore:explorer",
        "bluecore:planner",
        "bluecore:architect",
        "bluecore:tdd-writer",
        "bluecore:reviewer",
        "bluecore:security-auditor",
        "bluecore:simplifier",
        "bluecore:dead-code-cleaner",
        "bluecore:perf-optimizer",
        "bluecore:refactor-orchestrator",
    ])
    def test_agent_table_contains_all_agents(self, agent_name: str) -> None:
        """AGENT_TABLE に全 10 エージェント名を含む。"""
        assert agent_name in pre_agent_nudge.AGENT_TABLE


def test_main_entrypoint_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """__main__ として実行したとき SystemExit(0) で終了する。"""
    monkeypatch.setattr("sys.stdin.buffer.read", lambda n: b"")
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.pre_agent_nudge", run_name="__main__")
    assert excinfo.value.code == 0
