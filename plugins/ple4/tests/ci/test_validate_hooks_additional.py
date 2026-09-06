"""ple4.ci.validate_hooks の追加テスト。"""

from __future__ import annotations

import importlib
import json
import runpy
import shlex
import sys
from pathlib import Path

import pytest

validate_hooks = importlib.import_module("ple4.ci.validate_hooks")


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
    filler = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
    events: dict[str, object] = dict.fromkeys(validate_hooks.REQUIRED_EVENTS, filler)
    events["UserPromptSubmit"] = [{"hooks": [{"type": "prompt", "prompt": "ok"}]}]
    events["PreToolUse"] = [{"matcher": "tool", "hooks": [{"type": "command", "command": "echo ok"}]}]
    write_json(hooks_file, {"hooks": events})

    assert validate_hooks.validate_hooks(hooks_file) == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out
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

    filler = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
    events = dict.fromkeys(validate_hooks.REQUIRED_EVENTS, filler)
    events["UserPromptSubmit"] = [{"hooks": [{"type": "prompt", "prompt": "ok"}]}]
    write_json(hooks_file, {"hooks": events})

    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_hooks.py", "--hooks-file", str(hooks_file)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("ple4.ci.validate_hooks", run_name="__main__")

    assert excinfo.value.code == 0


def _repo_hook_argv() -> list[list[str]]:
    """実際の hooks.json の command フックを wrapper 以降の argv へ分解して返す。

    Returns:
        ``--bg`` を取り除いた argv のリスト（先頭がモジュール名）。
    """
    repo_root = Path(__file__).resolve().parents[4]
    hooks_file = repo_root / "plugins/ple4/hooks/hooks.json"
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
        launcher_index = next(i for i, part in enumerate(parts) if part.endswith("ple4-hook"))
        argv = parts[launcher_index + 1 :]
        argvs.append(argv[1:] if argv and argv[0] == "--bg" else argv)
    return argvs


def test_repo_hook_modules_are_importable() -> None:
    """hooks.json が起動する全モジュールが実在することを確認する。"""
    for argv in _repo_hook_argv():
        assert importlib.util.find_spec(argv[0]) is not None, f"モジュールが存在しません: {argv[0]}"


def test_every_repo_hook_entry_launches_the_ple4_hook_wrapper() -> None:
    """hooks.json の全エントリが `runtime/ple4-hook` を起動すること。

    CLAUDE.md は「`hooks.json` は裸の `python3` を呼ばない。全エントリが
    `runtime/ple4-hook` を起動し、インタプリタ解決はこの wrapper の単一責務」と
    規定する。これまでこの規則を守っていたのは `_repo_hook_argv` の `next(...)`
    が StopIteration になる副作用だけで、assert もメッセージも無く、
    `ple4-hook` で終わる任意のパスを受理していた。
    """
    repo_root = Path(__file__).resolve().parents[4]
    hooks = json.loads((repo_root / "plugins/ple4/hooks/hooks.json").read_text(encoding="utf-8"))
    commands = [
        hook.get("command", "")
        for event_hooks in hooks["hooks"].values()
        for matcher in event_hooks
        for hook in matcher.get("hooks", [])
        if hook.get("type") == "command"
    ]

    assert commands, "hooks.json に command フックがありません"
    for command in commands:
        launcher = shlex.split(command)[0]
        assert launcher == "${CLAUDE_PLUGIN_ROOT}/runtime/ple4-hook", (
            f"wrapper 以外を起動しています: {launcher!r}"
        )


def test_repo_hook_modules_propagate_exit_code_via_system_exit() -> None:
    """hooks.json が起動する全モジュールが `main()` の戻り値を SystemExit で伝えること。

    `launcher.py` の `_run_module_in_process` は `SystemExit` を捕まえてその
    `code` を返し、例外が上がらなければ `0` を返す。したがって `__main__` が
    `main()` を素で呼ぶモジュールでは、`main()` が 2（deny）を返しても launcher は
    0（allow）を返す — PreToolUse の「exit 2 = deny」契約が黙って壊れる。

    `launcher.py` は「全フックは SystemExit 経由で終了することを確認済み」と
    書いているが、その確認は一度きりの目視で検知器が無かった。ADR-0011 決定6 /
    ADR-0023 が対象とする「宣言はあるが CI で実測されない契約」にあたる。
    """
    import ast

    for argv in _repo_hook_argv():
        module = argv[0]
        spec = importlib.util.find_spec(module)
        assert spec is not None and spec.origin, module
        tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
        main_blocks = [
            node
            for node in tree.body
            if isinstance(node, ast.If)
            and ast.dump(node.test).find("__main__") != -1
        ]
        assert main_blocks, f"{module} に __main__ ブロックがありません"
        source = "\n".join(ast.unparse(stmt) for block in main_blocks for stmt in block.body)
        assert "SystemExit(main())" in source or "sys.exit(main())" in source, (
            f"{module} の __main__ が main() の戻り値を SystemExit で伝えていません: {source!r}"
        )


def test_repo_mem_cli_hooks_split_target_and_args() -> None:
    """実際の hooks.json の ple4.mem.cli 呼び出しが、launcher 直後に
    モジュール名・サブコマンドの順で並び、かつサブコマンドが CLI に実在する
    ことを確認する（``--bg`` が付く detach エントリも許容する）。
    """
    from ple4.mem.cli import _COMMAND_HANDLERS

    mem_cli_argv = [argv for argv in _repo_hook_argv() if argv[0] == "ple4.mem.cli"]
    assert mem_cli_argv
    for argv in mem_cli_argv:
        assert argv[1] in _COMMAND_HANDLERS, f"未定義のサブコマンド: {argv[1]}"


def test_validate_http_hook_without_optional_fields() -> None:
    """headers / allowedEnvVars が無い HTTP フックは検証を通る。"""
    from ple4.ci.validate_hooks import _validate_http_hook

    assert _validate_http_hook({"url": "https://example.com"}, "test-hook") is False
