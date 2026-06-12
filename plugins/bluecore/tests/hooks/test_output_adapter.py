"""bluecore.hooks.output_adapter のテスト。"""

from __future__ import annotations

import json

import pytest

from bluecore.hooks import output_adapter
from bluecore.lib import harness


@pytest.fixture(autouse=True)
def _clear_harness_cache(monkeypatch):
    """各テストでハーネス判定キャッシュと判定用環境変数をリセットする。"""
    for key in list(__import__("os").environ):
        if key.startswith(("CODEX_", "COPILOT_")) or key in {
            "CLAUDECODE",
            "PLUGIN_DATA",
            "CLAUDE_PLUGIN_ROOT",
        }:
            monkeypatch.delenv(key, raising=False)
    harness.detect_harness.cache_clear()
    yield
    harness.detect_harness.cache_clear()


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
            ("COPILOT_AGENT_PROMPT", "x"),  # copilot
        ],
    )
    def test_all_harnesses_emit_claude_compatible_format(self, monkeypatch, env_key, env_value):
        """全ハーネスで Claude 互換の hookSpecificOutput 形式を出力する。"""
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
