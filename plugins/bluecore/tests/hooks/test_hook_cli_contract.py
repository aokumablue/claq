"""DT-06: リポジトリ内 launcher.py 経由の hook 入出力契約。

インストール済み plugin や実 HOME には触れない。COPILOT_TEST=1 で
Copilot の deny JSON を選ばせる。
"""

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PLUGIN_ROOT / "src" / "bluecore" / "launcher.py"
_ISOLATED_ENV_PREFIXES = ("CODEX_", "GROK_", "COPILOT_")
_ISOLATED_ENV_KEYS = frozenset({
    "CLAUDECODE",
    "PLUGIN_DATA",
    "BLUECORE_HOME",
    # mem のデータ位置を決めるのはこちら（src/bluecore/mem/settings.py）。
    # BLUECORE_HOME は lib/core_utils.get_bluecore_dir() が読む別変数で、
    # これを消しても実 ~/.bluecore への書き込みは止まらなかった。
    "BLUECORE_DATA_PATH",
    "CLAUDE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
})


def _launcher_env(tmp_home: Path) -> dict[str, str]:
    """launcher サブプロセス用の隔離環境を組み立てる。

    Args:
        tmp_home: 一時 HOME。実ユーザーの ~/.bluecore を触らない。

    Returns:
        サブプロセスに渡す環境変数。
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(_ISOLATED_ENV_PREFIXES) or key in _ISOLATED_ENV_KEYS:
            del env[key]
    env["HOME"] = str(tmp_home)
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["COPILOT_TEST"] = "1"
    return env


def _run_launcher(hook: str, payload: dict, tmp_home: Path) -> subprocess.CompletedProcess[str]:
    """リポジトリ内 launcher で hook モジュールを実行する。

    Args:
        hook: dotted module name（例: bluecore.hooks.config_protection）。
        payload: stdin に渡す JSON オブジェクト。
        tmp_home: 一時 HOME。

    Returns:
        CompletedProcess。
    """
    return subprocess.run(
        [sys.executable, str(LAUNCHER), hook],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_home),
        timeout=30,
        check=False,
    )


def _assert_denied_ruff(result: subprocess.CompletedProcess[str]) -> None:
    """config_protection が ruff.toml を deny JSON・exit 2 で返したことを検証する。"""
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert parsed["permissionDecision"] == "deny"
    assert "ruff.toml" in parsed["permissionDecisionReason"]


def test_launcher_config_protection_denies_snake_case_payload(tmp_path: Path) -> None:
    """DT-06 #1: snake_case の ruff.toml 編集は deny JSON・exit 2。"""
    result = _run_launcher(
        "bluecore.hooks.config_protection",
        {"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_denies_native_camel_case_payload(tmp_path: Path) -> None:
    """DT-06 #2: native camelCase の ruff.toml 編集も同じ deny。"""
    result = _run_launcher(
        "bluecore.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_allows_unprotected_native_file(tmp_path: Path) -> None:
    """DT-06 #3: native camelCase の sample.py は deny なし。"""
    result = _run_launcher(
        "bluecore.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "sample.py"}},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == "" or "permissionDecision" not in result.stdout


HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"

# 各イベントの最小 valid payload。hooks.json 由来の全エントリを起動して
# 「壊れずに終わる」ことだけを見る（allow/deny の正しさは個別テストの担当）。
_MINIMAL_PAYLOADS = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
    "PreCompact": {"session_id": "test-session", "transcript_path": "/nonexistent/transcript.jsonl"},
    "SessionStart": {"session_id": "test-session", "source": "startup"},
    "SessionEnd": {"session_id": "test-session", "reason": "clear"},
}


def _iter_declared_commands() -> list[tuple[str, list[str], int]]:
    """hooks.json が宣言する (イベント名, launcher 引数, timeout) を列挙する。

    Returns:
        宣言順の (event, args, timeout) タプル。args は launcher.py 以降の引数。
    """
    declared = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    entries: list[tuple[str, list[str], int]] = []
    for event, groups in declared.items():
        for group in groups:
            for hook in group.get("hooks", []):
                tokens = shlex.split(hook["command"])
                launcher_index = next(i for i, t in enumerate(tokens) if t.endswith("launcher.py"))
                entries.append((event, tokens[launcher_index + 1 :], hook.get("timeout", 0)))
    return entries


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_target_is_importable(event: str, args: list[str], timeout: int) -> None:
    """hooks.json が起動する dotted module がすべて import 可能であること。

    モジュール名をテスト側にハードコードすると、hooks.json 側のリネームを
    素通りさせる（テストは緑のまま実 hook が死ぬ）。宣言を単一情報源にする。
    """
    module = next(arg for arg in args if arg.startswith("bluecore."))
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
        ("bluecore.hooks.block_no_verify", "git commit --no-verify -m x", True),
        ("bluecore.hooks.block_no_verify", "git commit -n -m x", True),
        ("bluecore.hooks.block_no_verify", "git commit -m x", False),
        ("bluecore.hooks.block_no_verify", "git log -n 5", False),
        # bash_config_protection: リンタ設定の弱体化
        ("bluecore.hooks.bash_config_protection", "echo x > ruff.toml", True),
        ("bluecore.hooks.bash_config_protection", "rm .eslintrc", True),
        ("bluecore.hooks.bash_config_protection", "sudo cp /tmp/x ruff.toml", True),
        ("bluecore.hooks.bash_config_protection", "cat ruff.toml", False),
        ("bluecore.hooks.bash_config_protection", "ls -la", False),
        # pre_bash_commit_quality: commit 以外は素通り
        ("bluecore.hooks.pre_bash_commit_quality", "ls -la", False),
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
        [sys.executable, str(LAUNCHER), "bluecore.mem.cli", "context"],
        input=json.dumps(_MINIMAL_PAYLOADS["SessionStart"]),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:400]
    assert json.loads(result.stdout) is not None
    assert (tmp_path / ".bluecore" / "mem.db").is_file()
