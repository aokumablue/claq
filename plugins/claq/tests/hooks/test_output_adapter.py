"""claq.hooks.output_adapter のテスト（host 分岐ゼロの合併出力。環境変数やハーネス値に関わらず出力は常に同一）。"""

from __future__ import annotations

import json

from claq.hooks import output_adapter


def test_emit_block_returns_exit_2_deny_json_and_stderr_reason() -> None:
    """常に exit 2 + stderr へ理由 + stdout へ permissionDecision: deny JSON を同時に返すこと。"""
    exit_code, stdout, stderr = output_adapter.emit_block("dangerous flag")

    assert (exit_code, json.loads(stdout), stderr) == (
        2,
        {"permissionDecision": "deny", "permissionDecisionReason": "dangerous flag"},
        "dangerous flag",
    )
