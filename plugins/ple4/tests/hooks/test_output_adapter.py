"""ple4.hooks.output_adapter のテスト。

host 分岐ゼロの合併出力（各 host が自分の読むキーだけを拾える JSON）を
検証する。host 判定は行わないため、環境変数やハーネス値に関わらず出力は
常に同一である。
"""

from __future__ import annotations

import json

from ple4.hooks import output_adapter


class TestAdaptContextOutput:
    """adapt_context_output のテスト。"""

    def test_emits_merged_top_level_and_hook_specific_output(self):
        """additionalContext（トップレベル）と hookSpecificOutput を同時に出す。"""
        result = output_adapter.adapt_context_output("SessionStart", "ctx")
        parsed = json.loads(result)
        assert parsed["additionalContext"] == "ctx"
        assert parsed["hookSpecificOutput"] == {
            "hookEventName": "SessionStart",
            "additionalContext": "ctx",
        }

    def test_user_prompt_submit_event_name(self):
        """イベント名が hookSpecificOutput.hookEventName に反映される。"""
        result = output_adapter.adapt_context_output("UserPromptSubmit", "abc")
        payload = json.loads(result)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert payload["hookSpecificOutput"]["additionalContext"] == "abc"
        assert payload["additionalContext"] == "abc"

    def test_non_ascii_is_not_escaped(self):
        """日本語を含む出力が \\uXXXX へエスケープされない。"""
        result = output_adapter.adapt_context_output("SessionStart", "圧縮済み")
        assert "圧縮済み" in result


class TestAdaptPreToolUseContextOutput:
    """adapt_pre_tool_use_context_output のテスト。"""

    def test_emits_merged_pre_tool_use_output(self):
        """PreToolUse イベント名で合併出力を返す。"""
        result = output_adapter.adapt_pre_tool_use_context_output("ctx")
        parsed = json.loads(result)
        assert parsed["additionalContext"] == "ctx"
        assert parsed["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
        assert parsed["hookSpecificOutput"]["additionalContext"] == "ctx"


class TestEmitBlock:
    """emit_block のテスト。"""

    def test_blocks_with_exit_2_and_stderr_reason(self):
        """常に exit code 2 + stderr へ理由を書く。"""
        exit_code, _stdout, stderr = output_adapter.emit_block("reason")
        assert exit_code == 2
        assert stderr == "reason"

    def test_stdout_carries_permission_decision_deny(self):
        """stdout には permissionDecision: deny JSON を同時に出す。"""
        exit_code, stdout, stderr = output_adapter.emit_block("dangerous flag")
        assert exit_code == 2
        assert stderr == "dangerous flag"
        assert json.loads(stdout) == {
            "permissionDecision": "deny",
            "permissionDecisionReason": "dangerous flag",
        }
