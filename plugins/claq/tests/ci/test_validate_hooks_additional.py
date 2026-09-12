"""claq.ci.validate_hooks の追加テスト。"""

from __future__ import annotations

import importlib
import json
import runpy
import shlex
import sys
from pathlib import Path

import pytest

validate_hooks = importlib.import_module("claq.ci.validate_hooks")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_validate_hook_entry_errors(capsys: pytest.CaptureFixture[str]) -> None:
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
            "version": validate_hooks.REQUIRED_HOOKS_VERSION,
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
    """トップレベルが配列なら version 検査より前に弾かれること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, [])

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "hooks.json はオブジェクトである必要があります" in capsys.readouterr().err


def test_validate_hooks_and_main_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hooks_file = tmp_path / "hooks.json"
    filler = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
    events: dict[str, object] = dict.fromkeys(validate_hooks.REQUIRED_EVENTS, filler)
    events["UserPromptSubmit"] = [{"hooks": [{"type": "prompt", "prompt": "ok"}]}]
    events["PreToolUse"] = [{"matcher": "tool", "hooks": [{"type": "command", "command": "echo ok"}]}]
    write_json(hooks_file, {"version": validate_hooks.REQUIRED_HOOKS_VERSION, "hooks": events})

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
            "version": validate_hooks.REQUIRED_HOOKS_VERSION,
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
    write_json(hooks_file, {"version": validate_hooks.REQUIRED_HOOKS_VERSION, "hooks": events})

    monkeypatch.setattr(
        sys,
        "argv",
        ["validate_hooks.py", "--hooks-file", str(hooks_file)],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.ci.validate_hooks", run_name="__main__")

    assert excinfo.value.code == 0


def _repo_hook_argv() -> list[list[str]]:
    """実際の hooks.json の command フックを wrapper 以降の argv へ分解して返す。

    Returns:
        ``--bg`` を取り除いた argv のリスト（先頭がモジュール名）。
    """
    repo_root = Path(__file__).resolve().parents[4]
    hooks_file = repo_root / "plugins/claq/hooks/hooks.json"
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
        launcher_index = next(i for i, part in enumerate(parts) if part.endswith("claq-hook"))
        argv = parts[launcher_index + 1 :]
        argvs.append(argv[1:] if argv and argv[0] == "--bg" else argv)
    return argvs


def test_repo_hook_modules_are_importable() -> None:
    """hooks.json が起動する全モジュールが実在することを確認する。"""
    for argv in _repo_hook_argv():
        assert importlib.util.find_spec(argv[0]) is not None, f"モジュールが存在しません: {argv[0]}"


def test_every_repo_hook_entry_launches_the_claq_hook_wrapper() -> None:
    """hooks.json の全エントリが `runtime/claq-hook` を起動すること。

    CLAUDE.md は「`hooks.json` は裸の `python3` を呼ばない。全エントリが
    `runtime/claq-hook`（`command` 経由の Windows は cmd.exe が PATHEXT で解決
    する同名の `.cmd`。PowerShell ホスト向けの明示宣言は次項）を起動し、
    インタプリタ解決はこの wrapper の単一責務」と規定する。ここが見るのは
    `command` 側で、`powershell` 側の起動対象は
    `TestRepoHooksJsonPowerShellParity` が別途照合する。これまでこの規則を守っていたのは `_repo_hook_argv` の `next(...)`
    が StopIteration になる副作用だけで、assert もメッセージも無く、
    `claq-hook` で終わる任意のパスを受理していた。
    """
    repo_root = Path(__file__).resolve().parents[4]
    hooks = json.loads((repo_root / "plugins/claq/hooks/hooks.json").read_text(encoding="utf-8"))
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
        assert launcher == "${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook", (
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
    """実際の hooks.json の claq.mem.cli 呼び出しが、launcher 直後に
    モジュール名・サブコマンドの順で並び、かつサブコマンドが CLI に実在する
    ことを確認する（``--bg`` が付く detach エントリも許容する）。
    """
    from claq.mem.cli import _COMMAND_HANDLERS

    mem_cli_argv = [argv for argv in _repo_hook_argv() if argv[0] == "claq.mem.cli"]
    assert mem_cli_argv
    for argv in mem_cli_argv:
        assert argv[1] in _COMMAND_HANDLERS, f"未定義のサブコマンド: {argv[1]}"


def test_validate_http_hook_without_optional_fields() -> None:
    """headers / allowedEnvVars が無い HTTP フックは検証を通る。"""
    from claq.ci.validate_hooks import _validate_http_hook

    assert _validate_http_hook({"url": "https://example.com"}, "test-hook") is False


# ---------------------------------------------------------------------------
# commit 4066923（Windows PowerShell 対応）が導入した契約
# ---------------------------------------------------------------------------


def _complete_events() -> dict[str, object]:
    """必須イベントを構造的に正しいダミーで埋めた hooks コンテナを返す。

    Returns:
        REQUIRED_EVENTS を全て含む、イベント名 -> マッチャー配列の辞書。
    """
    filler = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
    return dict.fromkeys(validate_hooks.REQUIRED_EVENTS, filler)


@pytest.mark.parametrize(
    "version_field",
    [
        pytest.param({}, id="missing"),
        pytest.param({"version": "1"}, id="str"),
        pytest.param({"version": 1.0}, id="float"),
        pytest.param({"version": True}, id="bool-true"),
        pytest.param({"version": 0}, id="zero"),
        pytest.param({"version": 2}, id="other-int"),
        pytest.param({"version": None}, id="null"),
    ],
)
def test_validate_hooks_rejects_non_conforming_version(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], version_field: dict
) -> None:
    """`version` が正確に int の 1 でなければ、他が完全でも失敗すること。

    `True == 1` が成り立つ Python では `version: true` が素通りする。実装が
    `isinstance(version, bool)` を先に見ているのはこのためで、bool-true の行が
    そのガードを検査する唯一のケースになる。
    """
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, {**version_field, "hooks": _complete_events()})

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "hooks.json の 'version' は 1 である必要があります" in capsys.readouterr().err


def test_validate_hooks_accepts_version_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`version: 1` を持つ完成形は通ること。"""
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, {"version": 1, "hooks": _complete_events()})

    assert validate_hooks.validate_hooks(hooks_file) == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out


@pytest.mark.parametrize(
    "hooks_value",
    [
        pytest.param(None, id="missing"),
        pytest.param([], id="array"),
        pytest.param("x", id="str"),
    ],
)
def test_validate_hooks_requires_hooks_object(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], hooks_value: object
) -> None:
    """`hooks` キーがイベント辞書でなければ失敗すること。

    かつては `data.hooks || data` 相当のフォールバックがあり、`hooks` を省いた
    「裸のイベント辞書」も受理していた。`version` を必須にした時点でこの形は
    表現不能（`version` がイベント名として検査される）になったため撤去済み。
    """
    hooks_file = tmp_path / "hooks.json"
    document: dict[str, object] = {"version": validate_hooks.REQUIRED_HOOKS_VERSION}
    if hooks_value is not None:
        document["hooks"] = hooks_value
    write_json(hooks_file, document)

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "'hooks' はイベント名をキーとするオブジェクトである必要があります" in capsys.readouterr().err


def test_validate_hooks_rejects_bare_event_map(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`hooks` ラッパー無しのイベント辞書は受理しないこと。

    JS バリデータ互換のフォールバックを撤去したことの検知器。復活させると
    `$schema` や `version` がイベント名として検査され、診断が意味を失う。
    """
    hooks_file = tmp_path / "hooks.json"
    write_json(hooks_file, {"version": validate_hooks.REQUIRED_HOOKS_VERSION, **_complete_events()})

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "'hooks' はイベント名をキーとするオブジェクトである必要があります" in capsys.readouterr().err


@pytest.mark.parametrize(
    "powershell",
    [
        pytest.param("", id="empty"),
        pytest.param("   ", id="blank"),
        pytest.param(123, id="int"),
        pytest.param(None, id="null"),
        pytest.param(True, id="bool"),
        pytest.param(["& x"], id="array"),
    ],
)
def test_validate_command_hook_rejects_non_string_powershell(
    capsys: pytest.CaptureFixture[str], powershell: object
) -> None:
    """command フックの `powershell` は空でない文字列に限ること。

    `command` と違い配列形式は受理しない（ホストは PowerShell へ 1 本の
    文字列として渡すため）。空値や数値を許すと POSIX 用 `command` へ
    フォールバックせず、Windows で全ツール呼び出しが hook error になる。
    """
    hook = {"type": "command", "command": "echo ok", "powershell": powershell}

    assert validate_hooks.validate_hook_entry(hook, "label") is True
    assert "label の 'powershell' は空でない文字列である必要があります" in capsys.readouterr().err


def test_validate_command_hook_accepts_valid_powershell() -> None:
    """有効な `powershell` を持つ command フックは通ること。"""
    hook = {
        "type": "command",
        "command": '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" claq.hooks.pre_compact',
        "powershell": '& "${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook.cmd" claq.hooks.pre_compact',
    }

    assert validate_hooks.validate_hook_entry(hook, "label") is False


def test_validate_command_hook_without_powershell_key_is_valid() -> None:
    """`powershell` キーが無い command フックは従来どおり通ること。"""
    assert validate_hooks.validate_hook_entry({"type": "command", "command": "echo ok"}, "label") is False


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param({"type": "http", "url": "https://example.com"}, id="http"),
        pytest.param({"type": "prompt", "prompt": "ok"}, id="prompt"),
    ],
)
def test_powershell_is_rejected_on_non_command_hooks(
    capsys: pytest.CaptureFixture[str], hook: dict
) -> None:
    """非 command フックに付いた `powershell` は診断付きで拒否されること。

    `async` と同じ扱い。ここが無いと http / prompt フックの `powershell` は
    無診断で捨てられ、Windows だけ静かに動かない — 本フィールドが解消した
    障害と同じ形が 1 階層下に残る。
    """
    assert validate_hooks.validate_hook_entry({**hook, "powershell": "& x"}, "label") is True
    assert "label では 'powershell' は command フックでのみサポートされています" in capsys.readouterr().err
