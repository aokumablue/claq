"""claq.hooks.output_adapter のテスト（host 分岐ゼロの合併出力。環境変数やハーネス値に関わらず出力は常に同一）。"""

from __future__ import annotations

import json

from claq.hooks import output_adapter


def test_adapt_deny_output_returns_deny_json() -> None:
    """stdout 用の permissionDecision: deny JSON を返すこと。"""
    stdout = output_adapter.adapt_deny_output("dangerous flag")

    assert json.loads(stdout) == {"permissionDecision": "deny", "permissionDecisionReason": "dangerous flag"}
