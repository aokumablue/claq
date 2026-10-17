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

from ._support import LAUNCHER, _iter_declared_commands, _launcher_env, _run_launcher


def _assert_denied_ruff(result: subprocess.CompletedProcess[str]) -> None:
    """config_protection が ruff.toml を deny JSON・exit 2 で返したことを検証する。"""
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert parsed["permissionDecision"] == "deny"
    assert "ruff.toml" in parsed["permissionDecisionReason"]


def test_launcher_config_protection_denies_snake_case_payload(tmp_path: Path) -> None:
    """snake_case の ruff.toml 編集は deny JSON・exit 2。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_denies_native_camel_case_payload(tmp_path: Path) -> None:
    """native camelCase の ruff.toml 編集も同じ deny。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_allows_unprotected_native_file(tmp_path: Path) -> None:
    """native camelCase の sample.py は deny なし。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "sample.py"}},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == "" or "permissionDecision" not in result.stdout


# 各イベントの最小 valid payload。hooks.json 由来の全エントリを起動して
# 「壊れずに終わる」ことだけを見る（allow/deny の正しさは個別テストの担当）。
_MINIMAL_PAYLOADS = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
    "PreCompact": {"session_id": "test-session", "transcript_path": "/nonexistent/transcript.jsonl"},
    "SessionStart": {"session_id": "test-session", "source": "startup"},
    "SessionEnd": {"session_id": "test-session", "reason": "clear"},
}


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_target_is_importable(event: str, args: list[str], timeout: int) -> None:
    """hooks.json が起動する dotted module がすべて import 可能であること。

    モジュール名をテスト側にハードコードすると、hooks.json 側のリネームを
    素通りさせる（テストは緑のまま実 hook が死ぬ）。宣言を単一情報源にする。
    """
    module = next(arg for arg in args if arg.startswith("claq."))
    assert importlib.util.find_spec(module) is not None, f"{event}: {module} が import できない"
    assert timeout > 0, f"{event}: timeout が宣言されていない"


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_runs_without_traceback(
    event: str, args: list[str], timeout: int, tmp_path: Path
) -> None:
    """hooks.json の全エントリが、最小 payload で例外を出さずに終了すること。

    「起動したら即クラッシュする」は静的 validator では原理的に検出できず、
    本番では保護の静かな無効化として現れる。
    """
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        input=json.dumps(_MINIMAL_PAYLOADS[event]),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert "Traceback" not in result.stderr, f"{event} {args}: {result.stderr[:400]}"
    assert result.returncode in {0, 2}, f"{event} {args}: exit={result.returncode}"


@pytest.mark.parametrize(
    ("hook", "command", "denied"),
    [
        # block_no_verify: git フックのバイパス
        ("claq.hooks.block_no_verify", "git commit --no-verify -m x", True),
        ("claq.hooks.block_no_verify", "git commit -n -m x", True),
        ("claq.hooks.block_no_verify", "git commit -m x", False),
        ("claq.hooks.block_no_verify", "git log -n 5", False),
        # bash_config_protection: リンタ設定の弱体化
        ("claq.hooks.bash_config_protection", "echo x > ruff.toml", True),
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

    in-process の判定関数テストは stdin JSON → exit code → deny JSON 出力までの
    経路を通らない。実際にホストが起動する形で往復させる。
    """
    result = _run_launcher(hook, {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    if denied:
        assert result.returncode == 2, f"{command}: exit={result.returncode}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
    else:
        assert result.returncode == 0, f"{command}: exit={result.returncode} err={result.stderr[:200]}"


def test_session_start_emits_valid_json_and_creates_db(tmp_path: Path) -> None:
    """SessionStart が空 DB から起動して有効な JSON を出し、DB を作ること。

    ここが壊れると全セッションの起動時に知識注入が静かに消える。
    """
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "claq.mem.cli", "context"],
        input=json.dumps(_MINIMAL_PAYLOADS["SessionStart"]),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:400]
    assert json.loads(result.stdout) is not None
    assert (tmp_path / ".claq" / "mem.db").is_file()
