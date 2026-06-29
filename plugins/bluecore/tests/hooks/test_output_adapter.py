"""bluecore.hooks.output_adapter のテスト。

実機検証（Copilot CLI v1.0.65 app.js 解析）に基づく回帰テストを含む:
- Copilot CLI: additionalContext をトップレベルで出力
- Claude Code: hookSpecificOutput ラッパー形式で出力
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from bluecore.hooks import output_adapter

_CLAUDE_EXPECTED = {
    "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "ctx",
    }
}


class TestAdaptContextOutput:
    """adapt_context_output のテスト。"""

    @pytest.mark.parametrize(
        ("env_key", "env_value"),
        [
            ("CLAUDECODE", "1"),
            ("PLUGIN_DATA", "/tmp/data"),  # codex
        ],
    )
    def test_claude_and_codex_emit_hook_specific_output_format(self, monkeypatch, env_key, env_value):
        """Claude / Codex は hookSpecificOutput 形式を出力する。"""
        monkeypatch.setenv(env_key, env_value)
        result = output_adapter.adapt_context_output("SessionStart", "ctx")
        assert json.loads(result) == _CLAUDE_EXPECTED

    def test_unknown_harness_emits_claude_format(self):
        """unknown ハーネスは Claude 形式（最も安全側）で出力する。"""
        result = output_adapter.adapt_context_output("SessionStart", "ctx")
        assert json.loads(result) == _CLAUDE_EXPECTED

    def test_unmapped_harness_falls_back_to_claude_format(self, monkeypatch):
        """テーブル未登録のハーネス値でも KeyError にせず Claude 形式へ倒す。"""
        monkeypatch.setattr(output_adapter, "detect_harness", lambda: "future-harness")
        result = output_adapter.adapt_context_output("SessionStart", "ctx")
        assert json.loads(result) == _CLAUDE_EXPECTED

    def test_user_prompt_submit_event_name(self, monkeypatch):
        """イベント名が出力に反映される。"""
        monkeypatch.setenv("CLAUDECODE", "1")
        result = output_adapter.adapt_context_output("UserPromptSubmit", "abc")
        payload = json.loads(result)
        assert payload["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert payload["hookSpecificOutput"]["additionalContext"] == "abc"


class TestCopilotContextOutput:
    """Copilot CLI 向け additionalContext トップレベル出力のテスト。"""

    def test_copilot_outputs_top_level_additional_context(self):
        """Copilot CLI は additionalContext をトップレベルで読む。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="copilot"):
            result = output_adapter.adapt_context_output("SessionStart", "slim style")
        parsed = json.loads(result)
        assert parsed["additionalContext"] == "slim style"
        assert "hookSpecificOutput" not in parsed

    def test_copilot_user_prompt_output_top_level(self):
        """UserPromptSubmit も同じトップレベル形式。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="copilot"):
            result = output_adapter.adapt_context_output("UserPromptSubmit", "reminder")
        parsed = json.loads(result)
        assert parsed["additionalContext"] == "reminder"
        assert "hookSpecificOutput" not in parsed

    def test_copilot_event_name_is_ignored_in_output(self):
        """Copilot CLI は hookEventName を参照しないため出力に含まない。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="copilot"):
            result = output_adapter.adapt_context_output("SessionStart", "x")
        parsed = json.loads(result)
        assert "hookEventName" not in parsed
        assert "hookEventName" not in json.dumps(parsed)


class TestClaudeContextOutput:
    """Claude Code 向け hookSpecificOutput 形式出力のテスト。"""

    def test_claude_outputs_hook_specific_output_wrapper(self):
        """Claude Code は hookSpecificOutput ラッパー形式を使う。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="claude"):
            result = output_adapter.adapt_context_output("SessionStart", "slim style")
        parsed = json.loads(result)
        assert "hookSpecificOutput" in parsed
        assert parsed["hookSpecificOutput"]["additionalContext"] == "slim style"
        assert parsed["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_claude_context_does_not_expose_top_level_additional_context(self):
        """Claude Code 形式ではトップレベルに additionalContext がない。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="claude"):
            result = output_adapter.adapt_context_output("SessionStart", "x")
        parsed = json.loads(result)
        assert "additionalContext" not in parsed


class TestUnknownAndCodexContextOutput:
    """unknown / Codex ハーネス向け Claude 形式フォールバックのテスト。"""

    def test_unknown_uses_claude_format(self):
        """unknown ハーネスは安全側として Claude 形式を使う。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="unknown"):
            result = output_adapter.adapt_context_output("SessionStart", "x")
        parsed = json.loads(result)
        assert "hookSpecificOutput" in parsed

    def test_codex_uses_claude_format(self):
        """Codex は未検証のため Claude 形式を維持する。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="codex"):
            result = output_adapter.adapt_context_output("SessionStart", "x")
        parsed = json.loads(result)
        assert "hookSpecificOutput" in parsed

    def test_unregistered_harness_falls_back_to_claude_format(self):
        """テーブルにないハーネスは Claude 形式へフォールバックする。"""
        with patch("bluecore.hooks.output_adapter.detect_harness", return_value="future_harness"):
            result = output_adapter.adapt_context_output("SessionStart", "x")
        parsed = json.loads(result)
        assert "hookSpecificOutput" in parsed


class TestEmitBlock:
    """emit_block のテスト。"""

    @pytest.mark.parametrize(
        ("env_key", "env_value"),
        [
            ("CLAUDECODE", "1"),
            ("PLUGIN_DATA", "/tmp/data"),  # codex は exit 2 ブロック互換
        ],
    )
    def test_claude_and_codex_block_with_exit_2(self, monkeypatch, env_key, env_value):
        """Claude / Codex は exit code 2 + stderr でブロックする。"""
        monkeypatch.setenv(env_key, env_value)
        assert output_adapter.emit_block("reason") == (2, "", "reason")

    def test_unknown_blocks_with_exit_2(self):
        """unknown ハーネスも exit code 2 でブロックする。"""
        assert output_adapter.emit_block("reason") == (2, "", "reason")

    def test_copilot_blocks_with_permission_decision_deny(self, monkeypatch):
        """Copilot は exit 2 が fail-open のため deny JSON + exit 0 に変換する。"""
        monkeypatch.setenv("COPILOT_AGENT_PROMPT", "x")
        exit_code, stdout, stderr = output_adapter.emit_block("dangerous flag")
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout) == {
            "permissionDecision": "deny",
            "permissionDecisionReason": "dangerous flag",
        }
