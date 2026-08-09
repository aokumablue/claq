"""bluecore.ci.validate_hooks の追加テスト。"""

from __future__ import annotations

import importlib
import json
import runpy
import shlex
import sys
from pathlib import Path

import pytest

validate_hooks = importlib.import_module("bluecore.ci.validate_hooks")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_select_hooks_container_and_validate_hook_entry_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert validate_hooks._select_hooks_container({"hooks": []}) == []
    original = {"hooks": None, "other": 1}
    assert validate_hooks._select_hooks_container(original) is original
    assert validate_hooks._select_hooks_container([]) == []
    assert validate_hooks._select_hooks_container({"hooks": False}) == {"hooks": False}
    assert validate_hooks._select_hooks_container({"hooks": 0}) == {"hooks": 0}

    assert validate_hooks.validate_hook_entry("bad", "label") is True
    stderr = capsys.readouterr().err
    assert "label は 'type' フィールドが不足しているか無効です" in stderr

    assert validate_hooks.validate_hook_entry({"type": "unknown"}, "label") is True
    assert validate_hooks.validate_hook_entry({"type": "command", "command": 1, "async": "no", "timeout": -1}, "label") is True
    assert validate_hooks.validate_hook_entry(
        {"type": "http", "url": "", "headers": {"a": 1}, "allowedEnvVars": [1], "async": True},
        "label",
    ) is True
    assert validate_hooks.validate_hook_entry({"type": "prompt", "prompt": "", "model": ""}, "label") is True
    assert validate_hooks.validate_hook_entry({"type": ""}, "label") is True
    assert (
        validate_hooks.validate_hook_entry(
            {"type": "command", "command": "echo ok", "async": False, "timeout": 0},
            "label",
        )
        is False
    )
    assert (
        validate_hooks.validate_hook_entry(
            {"type": "http", "url": "https://example.com", "headers": {"a": "b"}, "allowedEnvVars": ["A"]},
            "label",
        )
        is False
    )
    assert validate_hooks.validate_hook_entry({"type": "prompt", "prompt": "ok", "model": "sonnet"}, "label") is False

    stderr = capsys.readouterr().err
    assert "サポートされていないフックタイプ 'unknown'" in stderr
    assert "'async' は真偽値である必要があります" in stderr
    assert "'timeout' は 0 以上の数値である必要があります" in stderr
    assert "'command' フィールドが不足しているか無効です" in stderr
    assert "'url' フィールドが不足しているか無効です" in stderr
    assert "'headers' は文字列値を持つオブジェクトである必要があります" in stderr
    assert "'allowedEnvVars' は文字列の配列である必要があります" in stderr
    assert "では 'async' は command フックでのみサポートされています" in stderr
    assert "'prompt' フィールドが不足しているか無効です" in stderr
    assert "'model' は空でない文字列である必要があります" in stderr
    assert "label は 'type' フィールドが不足しているか無効です" in stderr


def test_validate_hooks_reports_top_level_and_event_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        {
            "hooks": {
                "PreToolUse": [{"matcher": {"kind": "x"}, "hooks": [{"type": "command", "command": 1}]}],
                "PermissionRequest": [{"hooks": []}],
                "PostToolUse": [{"matcher": "x", "hooks": {}}],
                "PostToolUseFailure": [123],
                "ConfigChange": {"matcher": "x"},
                "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
                "BadEvent": [{"matcher": "x", "hooks": []}],
                "Stop": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
            }
        },
    )

    assert validate_hooks.validate_hooks(hooks_file) == 1
    stderr = capsys.readouterr().err
    assert "PreToolUse[0] の 'hooks' 配列が不足しています" not in stderr
    assert "無効なイベントタイプ: BadEvent" in stderr
    assert "PreToolUse[0].hooks[0] は 'command' フィールドが不足しているか無効です" in stderr
    assert "UserPromptSubmit[0] は 'matcher' フィールドが不足しています" not in stderr
    assert "PermissionRequest[0] は 'matcher' フィールドが不足しています" in stderr
    assert "PostToolUse[0] は 'hooks' 配列が不足しています" in stderr
    assert "PostToolUseFailure[0] はオブジェクトではありません" in stderr
    assert "ConfigChange は配列である必要があります" in stderr


def test_validate_hooks_rejects_top_level_array(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, [])

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "hooks.json はオブジェクトまたは配列である必要があります" in capsys.readouterr().err


def test_validate_hooks_and_main_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
                "PreToolUse": [{"matcher": "tool", "hooks": [{"type": "command", "command": "echo ok"}]}],
            }
        },
    )

    assert validate_hooks.validate_hooks(hooks_file) == 0
    assert "2 個のフックマッチャーを検証しました" in capsys.readouterr().out
    assert validate_hooks.main(["--hooks-file", str(hooks_file)]) == 0


def test_validate_hooks_reports_invalid_matcher_and_entrypoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    hooks_file = tmp_path / "hooks.json"
    write_json(
        hooks_file,
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": 1,
                        "hooks": [{"type": "command", "command": "echo ok"}],
                    }
                ]
            }
        },
    )

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "の 'matcher' フィールドが無効です" in capsys.readouterr().err

    write_json(
        hooks_file,
        {
            "hooks": {
                "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
            }
        },
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_hooks.py", "--hooks-file", str(hooks_file)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.ci.validate_hooks", run_name="__main__")

    assert excinfo.value.code == 0


def _repo_hook_argv() -> list[list[str]]:
    """実際の hooks.json の command フックを launcher 以降の argv へ分解して返す。

    Returns:
        ``--bg`` を取り除いた argv のリスト（先頭がモジュール名）。
    """
    repo_root = Path(__file__).resolve().parents[4]
    hooks_file = repo_root / "plugins/bluecore/hooks/hooks.json"
    hooks = json.loads(hooks_file.read_text(encoding="utf-8"))
    commands = [
        hook.get("command", "")
        for event_hooks in hooks["hooks"].values()
        for matcher in event_hooks
        for hook in matcher.get("hooks", [])
        if hook.get("type") == "command"
    ]

    argvs: list[list[str]] = []
    for command in commands:
        parts = shlex.split(command)
        launcher_index = next(i for i, part in enumerate(parts) if part.endswith("launcher.py"))
        argv = parts[launcher_index + 1 :]
        argvs.append(argv[1:] if argv and argv[0] == "--bg" else argv)
    return argvs


def test_repo_hook_modules_are_importable() -> None:
    """hooks.json が起動する全モジュールが実在することを確認する。"""
    for argv in _repo_hook_argv():
        assert importlib.util.find_spec(argv[0]) is not None, f"モジュールが存在しません: {argv[0]}"


def test_repo_mem_cli_hooks_split_target_and_args() -> None:
    """実際の hooks.json の bluecore.mem.cli 呼び出しが、launcher 直後に
    モジュール名・サブコマンドの順で並び、かつサブコマンドが CLI に実在する
    ことを確認する（``--bg`` が付く detach エントリも許容する）。
    """
    from bluecore.mem.cli import _COMMAND_HANDLERS

    mem_cli_argv = [argv for argv in _repo_hook_argv() if argv[0] == "bluecore.mem.cli"]
    assert mem_cli_argv
    for argv in mem_cli_argv:
        assert argv[1] in _COMMAND_HANDLERS, f"未定義のサブコマンド: {argv[1]}"


def test_validate_http_hook_without_optional_fields() -> None:
    """headers / allowedEnvVars が無い HTTP フックは検証を通る。"""
    from bluecore.ci.validate_hooks import _validate_http_hook

    assert _validate_http_hook({"url": "https://example.com"}, "test-hook") is False
