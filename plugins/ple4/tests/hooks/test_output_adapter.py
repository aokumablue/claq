"""ple4.hooks.output_adapter のテスト。

host 分岐ゼロの合併出力（各 host が自分の読むキーだけを拾える JSON）を
検証する。host 判定は行わないため、環境変数やハーネス値に関わらず出力は
常に同一である。
"""

from __future__ import annotations

import json

from ple4.hooks import output_adapter


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
