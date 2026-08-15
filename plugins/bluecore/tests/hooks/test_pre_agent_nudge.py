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
        "bluecore:executor",
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
        """AGENT_TABLE に全 11 エージェント名を含む。"""
        assert agent_name in pre_agent_nudge.AGENT_TABLE


class TestPreAgentNudgeExplore:
    """Explore subagent_type 検知テスト。"""

    def test_explore_emits_hook_specific_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explore 検知時に hookSpecificOutput を stdout に書き出し、return 0。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore", "prompt": "hello"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert len(stdout) == 1
        parsed = json.loads(stdout[0])
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"

    def test_explore_context_contains_explore_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explore 検知時に additionalContext に EXPLORE_TABLE を含む。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        pre_agent_nudge.main()
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bluecore:explorer" in ctx

    def test_explore_lowercase_returns_0_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小文字 explore は非該当のため出力なし・return 0（大文字小文字厳密一致）。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "explore"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        result = pre_agent_nudge.main()
        assert result == 0
        assert stdout == []


class TestExploreTableRegression:
    """EXPLORE_TABLE 退行防止テスト。"""

    def test_explore_table_contains_bluecore_prefix(self) -> None:
        """EXPLORE_TABLE が [bluecore] 接頭辞を含む。"""
        assert pre_agent_nudge.EXPLORE_TABLE.startswith("[bluecore]")

    def test_explore_table_contains_explorer_agent(self) -> None:
        """EXPLORE_TABLE に bluecore:explorer を含む。"""
        assert "bluecore:explorer" in pre_agent_nudge.EXPLORE_TABLE


class TestPreAgentNudgeClaudeUnchanged:
    """H-04/A-01 対応で Grok 分岐を追加しても Claude 側の出力が 1 バイトも変わらないことの固定回帰テスト。"""

    def test_claudecode_general_purpose_output_matches_agent_table_exactly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLAUDECODE=1 環境で general-purpose の additionalContext は AGENT_TABLE と完全一致する。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert ctx == pre_agent_nudge.AGENT_TABLE


class TestPreAgentNudgeGrok:
    """Grok（spawn_subagent）向け分岐テスト。

    Grok の spawn_subagent が受理する型は explore（小文字）/ general-purpose /
    plan の 3 つのみで bluecore 専門エージェント型は存在しない（実機で
    "Unknown subagent type" エラーを確認済み）。conftest.py の共通フィクスチャが
    CLAUDE_PLUGIN_ROOT を delenv するため、GROK_* 環境変数を明示的に
    setenv して判定させる（plugin_root パスパターンには頼らない）。
    """

    def test_grok_general_purpose_message_has_no_bluecore_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GROK_AGENT=1 環境で subagent_type=general-purpose の出力に bluecore: を含まない。"""
        monkeypatch.setenv("GROK_AGENT", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bluecore:" not in ctx
        assert ctx == pre_agent_nudge.GROK_MESSAGE

    def test_grok_lowercase_explore_message_has_no_bluecore_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GROK_AGENT=1 環境で subagent_type=explore（小文字）の出力に bluecore: を含まない。"""
        monkeypatch.setenv("GROK_AGENT", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "explore"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bluecore:" not in ctx
        assert ctx == pre_agent_nudge.GROK_MESSAGE

    def test_grok_plan_message_has_no_bluecore_type(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GROK_AGENT=1 環境で subagent_type=plan の出力に bluecore: を含まない。"""
        monkeypatch.setenv("GROK_AGENT", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "plan"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        parsed = json.loads(stdout[0])
        ctx = parsed["hookSpecificOutput"]["additionalContext"]
        assert "bluecore:" not in ctx

    def test_grok_uppercase_explore_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GROK_AGENT=1 環境で大文字 Explore は Grok の受理型に含まれないため無出力。"""
        monkeypatch.setenv("GROK_AGENT", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "Explore"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        assert stdout == []

    def test_grok_unknown_subagent_type_no_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """GROK_AGENT=1 環境で未知の subagent_type は無出力。"""
        monkeypatch.setenv("GROK_AGENT", "1")
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"subagent_type": "bluecore:reviewer"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        assert stdout == []


def _assert_table_output(stdout: list[str], *, expected_agent: str) -> None:
    """対応表出力の PreToolUse JSON 契約を検証する。

    Args:
        stdout: write_stdout に渡された行。
        expected_agent: additionalContext に含まれるべきエージェント名。
    """
    assert len(stdout) == 1
    parsed = json.loads(stdout[0])
    hook = parsed["hookSpecificOutput"]
    assert hook["hookEventName"] == "PreToolUse"
    ctx = hook["additionalContext"]
    assert ctx
    assert expected_agent in ctx
    assert parsed.get("permissionDecision") != "deny"


class TestPreAgentNudgePayloadContracts:
    """DT-02: extract_tool_input 経由の agent_type / ネイティブ payload。"""

    def test_pre_agent_nudge_emits_agent_table_for_tool_input_agent_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-01: snake_case tool_input.agent_type=general-purpose で AGENT_TABLE。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"agent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:executor")

    def test_pre_agent_nudge_emits_agent_table_for_native_camel_case_payload(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """R-02: native toolName/toolArgs.agent_type=general-purpose で AGENT_TABLE。"""
        payload = json.dumps({
            "toolName": "agent",
            "toolArgs": {"agent_type": "general-purpose"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:executor")

    def test_pre_agent_nudge_emits_explore_table_for_agent_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """snake_case / native の agent_type=Explore で EXPLORE_TABLE。"""
        snake = json.dumps({
            "tool_name": "Agent",
            "tool_input": {"agent_type": "Explore"},
        })
        stdout, _ = _capture_io(monkeypatch, snake)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:explorer")

        native = json.dumps({
            "toolName": "agent",
            "toolArgs": {"agent_type": "Explore"},
        })
        stdout, _ = _capture_io(monkeypatch, native)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:explorer")

    def test_pre_agent_nudge_accepts_native_tool_args_json_string(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """native toolArgs が JSON 文字列でも AGENT_TABLE を出す。"""
        payload = json.dumps({
            "toolName": "agent",
            "toolArgs": json.dumps({"agent_type": "general-purpose"}),
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:executor")

    def test_pre_agent_nudge_prefers_subagent_type_when_both_keys_exist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同一 dict 内では subagent_type を agent_type より優先する。"""
        payload = json.dumps({
            "tool_name": "Agent",
            "tool_input": {
                "subagent_type": "Explore",
                "agent_type": "general-purpose",
            },
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        _assert_table_output(stdout, expected_agent="bluecore:explorer")
        assert "bluecore:executor" not in json.loads(stdout[0])["hookSpecificOutput"]["additionalContext"]

    def test_pre_agent_nudge_ignores_unknown_agent_type(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知の agent_type は無出力・exit 0。"""
        payload = json.dumps({
            "toolName": "agent",
            "toolArgs": {"agent_type": "custom-agent"},
        })
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        assert stdout == []

    @pytest.mark.parametrize(
        "payload",
        [
            json.dumps({"tool_args": {"agent_type": "general-purpose"}}),
            json.dumps({
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "Explore"},
                "toolArgs": {"agent_type": "general-purpose"},
            }),
            json.dumps({"toolName": "agent", "toolArgs": {}}),
            json.dumps({"toolName": "agent", "toolArgs": "not-a-dict"}),
            "{not-json",
            "",
            json.dumps({
                "toolName": "agent",
                "toolArgs": {"agent_type": "General-Purpose"},
            }),
            json.dumps({"toolName": "agent", "toolArgs": {"agent_type": "explore"}}),
        ],
        ids=[
            "dt02-7-tool_args-agent-table",
            "dt02-9-canonical-tool_input-wins",
            "dt02-11-missing-agent-type",
            "dt02-12-non-dict-toolargs",
            "dt02-13-invalid-json",
            "dt02-13-empty",
            "dt02-14-general-purpose-wrong-case",
            "dt02-14-explore-wrong-case",
        ],
    )
    def test_pre_agent_nudge_remaining_dt02_rows(
        self, monkeypatch: pytest.MonkeyPatch, payload: str
    ) -> None:
        """DT-02 残行: tool_args 吸収、混在優先、未知・不正・大文字小文字。"""
        stdout, _ = _capture_io(monkeypatch, payload)
        assert pre_agent_nudge.main() == 0
        if "tool_args" in payload and "general-purpose" in payload and "tool_input" not in payload:
            _assert_table_output(stdout, expected_agent="bluecore:executor")
        elif '"subagent_type": "Explore"' in payload and "toolArgs" in payload:
            _assert_table_output(stdout, expected_agent="bluecore:explorer")
        else:
            assert stdout == []


def test_main_entrypoint_exits_0(monkeypatch: pytest.MonkeyPatch) -> None:
    """__main__ として実行したとき SystemExit(0) で終了する。"""
    from bluecore.hooks import hook_common

    monkeypatch.setattr("sys.stdin.buffer.read", lambda n: b"")
    # pytest がキャプチャする stdin は実 fd を持たないため、
    # read_raw_stdin 内の select.select（_stdin_ready）を素通りさせる。
    monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: (r, [], []))
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.pre_agent_nudge", run_name="__main__")
    assert excinfo.value.code == 0
