"""ランチャー実行時とフック実行のテスト。

launcher.py の統合、モジュール解決、環境設定を対象とする。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from bluecore.hooks.doc_file_warning import is_suspicious_doc_path
from bluecore.hooks.run_with_flags import resolve_target_command

REPO_ROOT = Path(__file__).resolve().parents[4]
LAUNCHER = REPO_ROOT / "plugins" / "bluecore" / "src" / "bluecore" / "launcher.py"


def run_launcher(
    *args: str, input_text: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    if env:
        run_env.update(env)

    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        input=input_text,
        text=True,
        capture_output=True,
        cwd=REPO_ROOT,
        env=run_env,
        check=False,
    )


def test_launcher_runs_python_hook_and_preserves_payload() -> None:
    payload = json.dumps({"tool_input": {"file_path": "notes/TODO.md"}})
    result = run_launcher("bluecore.hooks.doc_file_warning", input_text=payload)

    assert result.returncode == 0
    assert result.stdout == ""
    assert "Ad-hoc documentation filename detected" in result.stderr


def test_run_with_flags_skips_disabled_hook() -> None:
    payload = json.dumps({"tool_input": {"command": "git commit --no-verify"}})
    result = run_launcher(
        "bluecore.hooks.run_with_flags",
        "test-hook",
        "bluecore.hooks.block_no_verify",
        "standard",
        input_text=payload,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_run_with_flags_skips_disabled_hook_without_truncating_large_stdin() -> None:
    payload = "a" * (1024 * 1024 + 128)
    result = run_launcher(
        "bluecore.hooks.run_with_flags",
        "test-hook",
        "bluecore.hooks.block_no_verify",
        "standard",
        input_text=payload,
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_run_with_flags_propagates_blocked_hook() -> None:
    payload = json.dumps({"tool_input": {"command": "git commit --no-verify"}})
    result = run_launcher(
        "bluecore.hooks.run_with_flags",
        "test-hook",
        "bluecore.hooks.block_no_verify",
        "strict",
        input_text=payload,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert "git hook bypass flags are not allowed" in result.stderr


@pytest.mark.parametrize(
    ("args", "input_text"),
    [
        (("bluecore.hooks.run_with_flags", "session:mem:setup", "bluecore.mem.cli", "minimal,standard,strict", "setup"), "{}"),
        (
            (
                "bluecore.hooks.run_with_flags",
                "session:mem:context",
                "bluecore.mem.cli",
                "strict",
                "context",
            ),
            json.dumps({"cwd": str(REPO_ROOT)}),
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "session:mem:record-project-profile",
                "bluecore.mem.cli",
                "standard,strict",
                "record-project-profile",
            ),
            json.dumps({"cwd": str(REPO_ROOT), "languages": ["python"], "frameworks": ["pytest"]}),
        ),
    ],
)
def test_session_start_mem_hooks_use_separate_target_args(
    args: tuple[str, ...], input_text: str
) -> None:
    result = run_launcher(*args, input_text=input_text)

    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "No module named" not in result.stderr
    assert "不明なコマンド" not in result.stderr
    if args[1] == "session:mem:setup":
        assert "コマンド setup 失敗" not in result.stderr


@pytest.mark.parametrize(
    ("args", "input_text"),
    [
        (
            (
                "bluecore.hooks.run_with_flags",
                "user:mem:session-init",
                "bluecore.mem.cli",
                "standard,strict",
                "session-init",
            ),
            json.dumps({"cwd": str(REPO_ROOT), "session_id": "s-user-init", "prompt": "hello"}),
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "user:team:session-init",
                "bluecore.mem.cli",
                "standard,strict",
                "team-session-init",
            ),
            json.dumps({"cwd": str(REPO_ROOT), "prompt": "hello"}),
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "user:mem:record-interaction",
                "bluecore.mem.cli",
                "standard,strict",
                "record-interaction",
            ),
            json.dumps({"cwd": str(REPO_ROOT), "user_prompt_full": "hello"}),
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "user:mem:sync-check",
                "bluecore.mem.cli",
                "standard,strict",
                "sync-check",
            ),
            "{}",
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "session:mem:end",
                "bluecore.mem.cli",
                "standard,strict",
                "session-end",
            ),
            json.dumps({"session_id": "s-session-end"}),
        ),
        (
            (
                "bluecore.hooks.run_with_flags",
                "session:mem:sync-check",
                "bluecore.mem.cli",
                "standard,strict",
                "sync-check",
            ),
            "{}",
        ),
    ],
)
def test_mem_cli_hooks_use_separate_target_args(
    args: tuple[str, ...], input_text: str
) -> None:
    result = run_launcher(*args, input_text=input_text)

    assert result.returncode == 0
    assert "No module named" not in result.stderr
    assert "不明なコマンド" not in result.stderr


def test_resolve_target_command_module_name() -> None:
    """モジュール名からコマンドを解決します。"""
    cmd = resolve_target_command("bluecore.hooks.doc_file_warning", ["arg1", "arg2"])
    assert cmd == [sys.executable, "-m", "bluecore.hooks.doc_file_warning", "arg1", "arg2"]


def test_resolve_target_command_module_name_no_args() -> None:
    """引数なしでモジュール名からコマンドを解決します。"""
    cmd = resolve_target_command("bluecore.hooks.doc_file_warning")
    assert cmd == [sys.executable, "-m", "bluecore.hooks.doc_file_warning"]


def test_resolve_target_command_absolute_python_script(tmp_path: Path) -> None:
    """絶対パスの Python スクリプトからコマンドを解決します。"""
    script = tmp_path / "test_script.py"
    script.write_text("print('test')")

    cmd = resolve_target_command(str(script), ["arg1"])
    assert cmd == [sys.executable, str(script), "arg1"]


def test_resolve_target_command_absolute_bash_script(tmp_path: Path) -> None:
    """絶対パスの Bash スクリプトからコマンドを解決します。"""
    script = tmp_path / "test_script.sh"
    script.write_text("#!/bin/bash\necho test")
    script.chmod(0o755)

    cmd = resolve_target_command(str(script))
    assert cmd == ["bash", str(script)]


def test_resolve_target_command_relative_script_in_plugin_root(tmp_path: Path) -> None:
    """CLAUDE_PLUGIN_ROOT 内の相対パス スクリプトを解決します。"""
    # プラグインルート内にスクリプトを作成
    script = tmp_path / "subdir" / "script.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("print('test')")

    # 相対パスで解決
    cmd = resolve_target_command("subdir/script.py", ["arg"], plugin_root=tmp_path)
    assert cmd == [sys.executable, str(script), "arg"]


def test_resolve_target_command_relative_path_escapes_plugin_root(tmp_path: Path) -> None:
    """CLAUDE_PLUGIN_ROOT を逃脱する相対パスはモジュール名として扱われます。"""
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir(parents=True, exist_ok=True)
    # プラグインルートの外にファイルを作成
    outside_file = tmp_path / "outside" / "script.py"
    outside_file.parent.mkdir(parents=True, exist_ok=True)
    outside_file.write_text("print('test')")

    # 相対パスでプラグインルートを逃脱しようとする
    escape_path = "../../outside/script.py"

    # resolve_target_command が存在しないパスをモジュール名として処理することを確認
    cmd = resolve_target_command(escape_path, ["arg"], plugin_root=plugin_root)
    # モジュール名として処理される（外部ファイルへのアクセスは拒否）
    assert cmd == [sys.executable, "-m", escape_path, "arg"]


def test_resolve_target_command_relative_nonexistent_path_as_module() -> None:
    """存在しない相対パスはモジュール名として処理されます。"""
    plugin_root = Path("/nonexistent/plugin")
    # 存在しないパスはモジュール名として処理される
    cmd = resolve_target_command("nonexistent.module", ["arg"], plugin_root=plugin_root)
    assert cmd == [sys.executable, "-m", "nonexistent.module", "arg"]


def test_run_with_flags_forwards_extra_args(tmp_path: Path) -> None:
    script = tmp_path / "echo_args.py"
    script.write_text(
        "\n".join(
            [
                "import json",
                "import sys",
                "payload = sys.stdin.read()",
                "print(json.dumps({'args': sys.argv[1:], 'stdin': payload}))",
            ]
        ),
        encoding="utf-8",
    )

    result = run_launcher(
        "bluecore.hooks.run_with_flags",
        "test-hook",
        str(script),
        "strict",
        "alpha",
        "beta",
        input_text="payload",
    )

    data = json.loads(result.stdout)

    assert result.returncode == 0
    assert data["args"] == ["alpha", "beta"]
    assert data["stdin"] == "payload"


def test_doc_file_warning_treats_gitlab_dir_as_structured() -> None:
    assert not is_suspicious_doc_path(".gitlab/NOTES.md")


def test_read_raw_stdin_no_truncation(monkeypatch) -> None:
    """入力が上限以下なら truncated=False で返す。"""
    from types import SimpleNamespace

    from bluecore.hooks import run_with_flags

    monkeypatch.setattr(run_with_flags.sys, "stdin", SimpleNamespace(read=lambda n: "short"))
    text, truncated = run_with_flags.read_raw_stdin_with_truncation()
    assert text == "short"
    assert truncated is False


def test_command_for_existing_file_executable(tmp_path) -> None:
    """拡張子なしの実行可能ファイルはそのまま起動コマンドにする。"""
    from bluecore.hooks.run_with_flags import _command_for_existing_file

    f = tmp_path / "tool"
    f.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    f.chmod(0o755)
    assert _command_for_existing_file(f, ["arg"]) == [str(f), "arg"]


def test_resolve_relative_noncommand_falls_back(tmp_path) -> None:
    """plugin_root 配下だが起動方法不明なファイルは python -m へフォールバック。"""
    from bluecore.hooks.run_with_flags import resolve_target_command

    (tmp_path / "plain").write_text("x", encoding="utf-8")  # 拡張子なし・非実行
    assert resolve_target_command("plain", plugin_root=tmp_path) == [sys.executable, "-m", "plain"]


def test_resolve_absolute_missing_falls_back(tmp_path) -> None:
    """絶対パスで不在なら python -m へフォールバック。"""
    from bluecore.hooks.run_with_flags import resolve_target_command

    missing = str(tmp_path / "nope_abs")
    assert resolve_target_command(missing, plugin_root=tmp_path)[:2] == [sys.executable, "-m"]


def test_resolve_absolute_noncommand_falls_back(tmp_path) -> None:
    """絶対パス存在だが起動方法不明なら python -m へフォールバック。"""
    from bluecore.hooks.run_with_flags import resolve_target_command

    f = tmp_path / "plainabs"
    f.write_text("x", encoding="utf-8")
    assert resolve_target_command(str(f), plugin_root=tmp_path)[:2] == [sys.executable, "-m"]


def test_run_target_session_start_nonzero_returns_zero(monkeypatch) -> None:
    """SessionStart フックは子プロセスが非0終了でも 0 を返す。"""
    from types import SimpleNamespace

    from bluecore.hooks import run_with_flags

    hook_id = next(iter(run_with_flags.SESSION_START_HOOK_IDS))
    monkeypatch.setattr(run_with_flags.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="", stderr="", returncode=1))
    monkeypatch.setattr(run_with_flags, "emit_session_start_output", lambda: "out")
    monkeypatch.setattr(run_with_flags, "write_stdout", lambda x: None)
    monkeypatch.setattr(run_with_flags, "write_stderr", lambda x: None)
    monkeypatch.setattr(run_with_flags, "build_env", lambda: {})
    assert run_with_flags._run_target(hook_id, "target", [], "raw") == 0
