"""launcher 経由の hook 起動・入出力往復のスモーク。

インストール済み plugin や実 HOME には触れない。COPILOT_TEST=1 で
Copilot の deny JSON を選ばせる。hooks.json が宣言する全エントリが
最小 payload で壊れずに終わること、Bash 系ブロック hook の allow/deny が
プロセス経路でも成り立つこと、SessionStart が DB を作って有効な JSON を
出すことを見る。PreToolUse の入力軸ゲートと実装照合は
test_hook_cli_contract.py の担当。
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ._support import LAUNCHER, _bash, _iter_declared_commands, _launcher_env, _run_launcher


def _launch(args: list[str], payload: dict, tmp_home: Path) -> subprocess.CompletedProcess[str]:
    """リポジトリ内 launcher へ args をそのまま渡し、payload を stdin へ流して実行する。"""
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_home),
        timeout=60,
        check=False,
    )


def _assert_round_trip(result: subprocess.CompletedProcess[str], reason_fragment: str | None) -> None:
    """reason_fragment があれば exit 2 とそれを理由に含む deny JSON、None なら出力なしの exit 0 であることを確かめる。"""
    if reason_fragment is None:
        assert (result.returncode, result.stdout) == (0, ""), result.stderr[:200]
        return
    assert result.returncode == 2, result.stderr[:200]
    parsed = json.loads(result.stdout)
    assert parsed["permissionDecision"] == "deny"
    assert reason_fragment in parsed["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("payload", "reason_fragment"),
    [
        ({"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}}, "ruff.toml"),
        ({"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}}, "ruff.toml"),
        ({"toolName": "edit", "toolArgs": {"file_path": "sample.py"}}, None),
    ],
    ids=["snake-case-deny", "native-camel-case-deny", "native-camel-case-unprotected-allow"],
)
def test_launcher_config_protection_payload_forms(payload: dict, reason_fragment: str | None, tmp_path: Path) -> None:
    """snake_case と native camelCase の ruff.toml 編集は同じ deny、native camelCase の sample.py は出力なしで通ること。"""
    _assert_round_trip(_run_launcher("claq.hooks.config_protection", payload, tmp_path), reason_fragment)


# 各イベントの最小 valid payload。hooks.json 由来の全エントリを起動して
# 「壊れずに終わる」ことだけを見る（allow/deny の正しさは個別テストの担当）。
_MINIMAL_PAYLOADS = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
    "PreCompact": {"session_id": "test-session", "transcript_path": "/nonexistent/transcript.jsonl"},
    "SessionStart": {"session_id": "test-session", "source": "startup"},
    "SessionEnd": {"session_id": "test-session", "reason": "clear"},
}


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_runs_without_traceback(event: str, args: list[str], timeout: int, tmp_path: Path) -> None:
    """hooks.json の全エントリが import 可能な dotted module と timeout を宣言し、最小 payload で例外を出さずに終わること。

    モジュール名をテスト側に書くと hooks.json 側のリネームを素通りさせる（テストは緑のまま実 hook が死ぬ）ため、
    宣言を単一情報源にする。「起動したら即クラッシュする」は静的 validator では原理的に検出できず、本番では保護の
    静かな無効化として現れる。
    """
    module = next(arg for arg in args if arg.startswith("claq."))
    assert importlib.util.find_spec(module) is not None, f"{event}: {module} が import できない"
    assert timeout > 0, f"{event}: timeout が宣言されていない"

    result = _launch(args, _MINIMAL_PAYLOADS[event], tmp_path)

    assert "Traceback" not in result.stderr, f"{event} {args}: {result.stderr[:400]}"
    assert result.returncode in {0, 2}, f"{event} {args}: exit={result.returncode}"


@pytest.mark.parametrize(
    ("hook", "command", "denied"),
    [
        # block_no_verify: git フックのバイパス
        ("claq.hooks.block_no_verify", "git commit -n -m x", True),
        ("claq.hooks.block_no_verify", "git log -n 5", False),
        # bash_config_protection: リンタ設定の弱体化
        ("claq.hooks.bash_config_protection", "rm .eslintrc", True),
        ("claq.hooks.bash_config_protection", "sudo cp /tmp/x ruff.toml", True),
        ("claq.hooks.bash_config_protection", "cat ruff.toml", False),
        ("claq.hooks.bash_config_protection", "ls -la", False),
        # pre_bash_commit_quality: commit 以外は素通り
        ("claq.hooks.pre_bash_commit_quality", "ls -la", False),
    ],
)
def test_bash_hook_allow_deny_matrix(hook: str, command: str, denied: bool, tmp_path: Path) -> None:
    """Bash 系ブロック hook が、プロセス経路でも期待どおり allow/deny すること。

    in-process の判定関数テストは stdin JSON → exit code → deny JSON 出力までの経路を通らない。実際にホストが起動する
    形で往復させる。`git commit --no-verify -m x` / `git commit -m x` / `echo x > ruff.toml` は
    test_hook_cli_contract.py の形状ゲート（dict / tool_input）が同じ payload で流す。
    """
    _assert_round_trip(_run_launcher(hook, _bash(command), tmp_path), "BLOCKED" if denied else None)


def test_session_start_emits_valid_json_and_creates_db(tmp_path: Path) -> None:
    """SessionStart が空 DB から起動して有効な JSON を出し、DB を作ること（壊れると全セッションの起動時の知識注入が静かに消える）。"""
    result = _launch(["claq.mem.cli", "context"], _MINIMAL_PAYLOADS["SessionStart"], tmp_path)

    assert result.returncode == 0, result.stderr[:400]
    assert json.loads(result.stdout) is not None
    assert (tmp_path / ".claq" / "mem.db").is_file()
