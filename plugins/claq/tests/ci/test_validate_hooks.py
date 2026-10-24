"""claq.ci.validate_hooks と実リポジトリの hooks/hooks.json のテスト。"""

from __future__ import annotations

import ast
import importlib.util
import json
import runpy
import shlex
import sys
from pathlib import Path

import pytest

from claq.ci import validate_hooks
from claq.ci.ci_common import REPO_ROOT
from claq.ci.validate_hooks import REQUIRED_EVENTS, REQUIRED_HOOKS_VERSION

REPO_HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

# 必須イベント（PreToolUse / PreCompact / SessionStart / SessionEnd）を構造的に正しいダミーで埋めたもの。
# validate_hooks は完成した hooks マニフェストを検証する契約なので、1 つのマッチャーだけを見たい
# テストでも残りはこれで埋める。
_FILLER = [{"matcher": "*", "hooks": [{"type": "command", "command": "true", "timeout": 5}]}]
_EVENTS: dict[str, object] = dict.fromkeys(REQUIRED_EVENTS, _FILLER)


def _write_json(path: Path, value: object) -> Path:
    """value を JSON として path へ書き出し、path を返す。"""
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return path


def test_validate_hooks_missing_file_fails_unless_optional(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """hooks.json が無ければ失敗し、optional=True のときだけスキップして 0 を返すこと。

    「見つからないのでスキップします」と表示して 0 を返すと、
    宣言がまるごと失われた破損を成功として報告してしまう。
    """
    assert validate_hooks.validate_hooks(tmp_path / "hooks.json") == 1
    assert "見つかりません" in capsys.readouterr().err
    assert validate_hooks.validate_hooks(tmp_path / "hooks.json", optional=True) == 0
    assert "検証をスキップします" in capsys.readouterr().out


def test_validate_hooks_rejects_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """不正な JSON は即座に失敗すること。"""
    hooks_file = tmp_path / "hooks.json"
    hooks_file.write_text("{ invalid json", encoding="utf-8")

    assert validate_hooks.validate_hooks(hooks_file) == 1
    assert "JSON 形式が不正です" in capsys.readouterr().err


_VERSION_ERROR = "hooks.json の 'version' は 1 である必要があります"
_HOOKS_ERROR = "'hooks' はイベント名をキーとするオブジェクトである必要があります"


@pytest.mark.parametrize(
    ("document", "message"),
    [
        # トップレベルが配列なら version 検査より前に弾く。
        pytest.param([], "hooks.json はオブジェクトである必要があります", id="top-level-array"),
        pytest.param({"hooks": _EVENTS}, _VERSION_ERROR, id="version-missing"),
        pytest.param({"version": "1", "hooks": _EVENTS}, _VERSION_ERROR, id="version-str"),
        pytest.param({"version": 1.0, "hooks": _EVENTS}, _VERSION_ERROR, id="version-float"),
        # True == 1 が成り立つため、isinstance(version, bool) のガードを検査する唯一の行。
        pytest.param({"version": True, "hooks": _EVENTS}, _VERSION_ERROR, id="version-bool-true"),
        pytest.param({"version": 0, "hooks": _EVENTS}, _VERSION_ERROR, id="version-zero"),
        pytest.param({"version": 2, "hooks": _EVENTS}, _VERSION_ERROR, id="version-other-int"),
        pytest.param({"version": None, "hooks": _EVENTS}, _VERSION_ERROR, id="version-null"),
        # data.hooks || data 相当のフォールバックは持たない。
        pytest.param({"version": 1}, _HOOKS_ERROR, id="hooks-missing"),
        pytest.param({"version": 1, "hooks": []}, _HOOKS_ERROR, id="hooks-array"),
        pytest.param({"version": 1, "hooks": "x"}, _HOOKS_ERROR, id="hooks-str"),
        # hooks ラッパー無しを受理すると $schema や version がイベント名として検査され、診断が意味を失う。
        pytest.param({"version": 1, **_EVENTS}, _HOOKS_ERROR, id="bare-event-map"),
        # {"hooks": {}} を「0 個検証しました」と成功扱いしない。
        pytest.param({"version": 1, "hooks": {}}, "必須イベントが宣言されていません", id="hooks-empty"),
    ],
)
def test_validate_hooks_rejects_malformed_document(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], document: object, message: str
) -> None:
    """トップレベル・version・hooks コンテナ・必須イベントのどれかが契約に合わなければ失敗すること。"""
    assert validate_hooks.validate_hooks(_write_json(tmp_path / "hooks.json", document)) == 1
    assert message in capsys.readouterr().err


def _pre_tool_use(command: str) -> dict[str, object]:
    """command フック 1 つだけを持つ PreToolUse マッチャーを返す。"""
    return {"PreToolUse": [{"matcher": "test", "hooks": [{"type": "command", "command": command}]}]}


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({}, id="filler-only"),
        pytest.param(
            _pre_tool_use('node -e "console.log(1+2)"'),
            id="command-string",
        ),
        # 旧式のインライン JS コマンドは不透明な文字列として受け入れる。
        pytest.param(
            _pre_tool_use('node -e "function {"'),
            id="inline-js-opaque",
        ),
        pytest.param(
            {
                "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
                "PreToolUse": [{"matcher": "tool", "hooks": [{"type": "command", "command": "echo ok"}]}],
            },
            id="event-without-matcher",
        ),
    ],
)
def test_validate_hooks_accepts_complete_manifest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], overrides: dict[str, object]
) -> None:
    """`version: 1` と全必須イベントを持つ完成形は通り、マッチャー数を報告すること。"""
    events = {**_EVENTS, **overrides}
    hooks_file = _write_json(tmp_path / "hooks.json", {"version": REQUIRED_HOOKS_VERSION, "hooks": events})

    assert validate_hooks.validate_hooks(hooks_file) == 0
    assert f"{len(events)} 個のフックマッチャーを検証しました" in capsys.readouterr().out


def test_validate_hooks_reports_event_and_matcher_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """イベント名・イベント配列・マッチャー・hooks 配列の誤りをそれぞれのラベル付きで報告すること。"""
    hooks_file = _write_json(
        tmp_path / "hooks.json",
        {
            "version": REQUIRED_HOOKS_VERSION,
            "hooks": {
                "PreToolUse": [{"matcher": {"kind": "x"}, "hooks": [{"type": "command", "command": 1}]}],
                "PreCompact": [{"matcher": 1, "hooks": [{"type": "command", "command": "echo ok"}]}],
                "PermissionRequest": [{"hooks": []}],
                "PostToolUse": [{"matcher": "x", "hooks": {}}],
                "PostToolUseFailure": [123],
                "ConfigChange": {"matcher": "x"},
                "UserPromptSubmit": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
                "BadEvent": [{"matcher": "x", "hooks": []}],
                "Stop": [{"hooks": [{"type": "prompt", "prompt": "ok"}]}],
            },
        },
    )

    assert validate_hooks.validate_hooks(hooks_file) == 1
    stderr = capsys.readouterr().err
    assert "PreToolUse[0] の 'hooks' 配列が不足しています" not in stderr
    assert "無効なイベントタイプ: BadEvent" in stderr
    assert "PreToolUse[0].hooks[0] は 'command' フィールドが不足しているか無効です" in stderr
    assert "PreCompact[0] の 'matcher' フィールドが無効です" in stderr
    assert "UserPromptSubmit[0] は 'matcher' フィールドが不足しています" not in stderr
    assert "PermissionRequest[0] は 'matcher' フィールドが不足しています" in stderr
    assert "PostToolUse[0] は 'hooks' 配列が不足しています" in stderr
    assert "PostToolUseFailure[0] はオブジェクトではありません" in stderr
    assert "ConfigChange は配列である必要があります" in stderr


def test_validate_hooks_main_and_module_entrypoint(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """main() と `python -m` 実行の両方が完成形の hooks.json で 0 を返すこと。"""
    hooks_file = _write_json(tmp_path / "hooks.json", {"version": REQUIRED_HOOKS_VERSION, "hooks": _EVENTS})

    assert validate_hooks.main(["--hooks-file", str(hooks_file)]) == 0
    assert "個のフックマッチャーを検証しました" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", ["validate_hooks.py", "--hooks-file", str(hooks_file)])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.ci.validate_hooks", run_name="__main__")
    assert excinfo.value.code == 0


# --- 単一フックエントリ -------------------------------------------------------

_POWERSHELL_ERROR = "label の 'powershell' は空でない文字列である必要があります"
_POWERSHELL_COMMAND_ONLY = "label では 'powershell' は command フックでのみサポートされています"


@pytest.mark.parametrize(
    ("hook", "messages"),
    [
        pytest.param("bad", ["label は 'type' フィールドが不足しているか無効です"], id="not-object"),
        pytest.param({"type": ""}, ["label は 'type' フィールドが不足しているか無効です"], id="empty-type"),
        pytest.param({"type": "unknown"}, ["サポートされていないフックタイプ 'unknown'"], id="unknown-type"),
        pytest.param(
            {"type": "command", "command": 1, "async": "no", "timeout": -1},
            [
                "'async' は真偽値である必要があります",
                "'timeout' は 0 以上の数値である必要があります",
                "'command' フィールドが不足しているか無効です",
            ],
            id="command-bad-fields",
        ),
        pytest.param(
            {"type": "http", "url": "", "headers": {"a": 1}, "allowedEnvVars": [1], "async": True},
            [
                "'url' フィールドが不足しているか無効です",
                "'headers' は文字列値を持つオブジェクトである必要があります",
                "'allowedEnvVars' は文字列の配列である必要があります",
                "では 'async' は command フックでのみサポートされています",
            ],
            id="http-bad-fields",
        ),
        pytest.param(
            {"type": "prompt", "prompt": "", "model": ""},
            ["'prompt' フィールドが不足しているか無効です", "'model' は空でない文字列である必要があります"],
            id="prompt-bad-fields",
        ),
        # powershell は空でない文字列に限る。配列は受理しない（ホストは 1 本の文字列として渡す）。
        # 空値や数値を許すと command へフォールバックせず、Windows で全ツール呼び出しが hook error になる。
        *(
            pytest.param({"type": "command", "command": "echo ok", "powershell": value}, [_POWERSHELL_ERROR], id=name)
            for name, value in [
                ("powershell-empty", ""),
                ("powershell-blank", "   "),
                ("powershell-int", 123),
                ("powershell-null", None),
                ("powershell-bool", True),
                ("powershell-array", ["& x"]),
            ]
        ),
        # 非 command フックの powershell を拒否しないと無診断で捨てられ、Windows だけ静かに動かない。
        pytest.param(
            {"type": "http", "url": "https://example.com", "powershell": "& x"},
            [_POWERSHELL_COMMAND_ONLY],
            id="powershell-on-http",
        ),
        pytest.param(
            {"type": "prompt", "prompt": "ok", "powershell": "& x"}, [_POWERSHELL_COMMAND_ONLY], id="powershell-on-prompt"
        ),
    ],
)
def test_hook_entry_errors_rejects(hook: object, messages: list[str]) -> None:
    """不正なフックエントリは該当フィールドの誤りを全て返すこと。"""
    errors = "\n".join(validate_hooks._hook_entry_errors(hook, "label"))

    for message in messages:
        assert message in errors


@pytest.mark.parametrize(
    "hook",
    [
        pytest.param({"type": "command", "command": "echo ok"}, id="command-minimal"),
        pytest.param({"type": "command", "command": "echo ok", "async": False, "timeout": 0}, id="command-full"),
        pytest.param(
            {
                "type": "command",
                "command": '"${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook" claq.hooks.pre_compact',
                "powershell": '& "${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook.cmd" claq.hooks.pre_compact',
            },
            id="command-with-powershell",
        ),
        pytest.param({"type": "http", "url": "https://example.com"}, id="http-minimal"),
        pytest.param(
            {"type": "http", "url": "https://example.com", "headers": {"a": "b"}, "allowedEnvVars": ["A"]},
            id="http-full",
        ),
        pytest.param({"type": "prompt", "prompt": "ok", "model": "sonnet"}, id="prompt-full"),
    ],
)
def test_hook_entry_errors_accepts(hook: dict[str, object]) -> None:
    """正しいフックエントリは誤りを 1 つも返さないこと。"""
    assert list(validate_hooks._hook_entry_errors(hook, "label")) == []


# --- 実リポジトリの hooks/hooks.json --------------------------------------------


def _repo_hooks_document() -> dict:
    """実リポジトリの hooks.json を読み込んで返す。"""
    return json.loads(REPO_HOOKS_JSON.read_text(encoding="utf-8"))


def _repo_command_hooks() -> list[tuple[str, dict]]:
    """実 hooks.json の全 command フックを (イベント名, フック) で返す。"""
    return [
        (event, hook)
        for event, entries in _repo_hooks_document()["hooks"].items()
        for entry in entries
        for hook in entry.get("hooks", [])
        if hook.get("type") == "command"
    ]


def _repo_hook_argv() -> list[list[str]]:
    """実 hooks.json の command フックを wrapper 以降の argv（``--bg`` 除去済み、先頭がモジュール名）で返す。"""
    argvs = [shlex.split(hook["command"])[1:] for _, hook in _repo_command_hooks()]
    return [argv[1:] if argv[0] == "--bg" else argv for argv in argvs]


class TestRepoHooksJsonStaticChecks:
    """実リポジトリの hooks/hooks.json に対する静的検証。

    実 install/update + host 登録の smoke test はこのテストの範囲を
    大きく超えるため実施しない。代わりに、hooks.json 自体が全経路を宣言し、
    かつ全エントリが timeout を持つことを静的に検証する。
    """

    _REQUIRED_PRE_TOOL_USE_MODULES = (
        "claq.hooks.block_no_verify",
        "claq.hooks.pre_bash_commit_quality",
        "claq.hooks.bash_config_protection",
        "claq.hooks.config_protection",
    )
    _REQUIRED_EVENTS = ("PreToolUse", "PreCompact", "SessionStart", "SessionEnd")

    def test_declares_valid_schema(self, capsys: pytest.CaptureFixture[str]) -> None:
        """validate_hooks（version: 1 を含む構造検証）が実 hooks.json に対して通ること。"""
        assert validate_hooks.validate_hooks(REPO_HOOKS_JSON) == 0
        assert "検証しました" in capsys.readouterr().out

    def test_declares_all_required_events(self) -> None:
        """必須イベントが全て空でないマッチャー配列で宣言されていること。"""
        hooks = _repo_hooks_document()["hooks"]
        assert [event for event in self._REQUIRED_EVENTS if not hooks.get(event)] == []

    def test_pre_tool_use_declares_all_required_modules(self) -> None:
        """PreToolUse が保護フックのモジュールを全て起動すること。"""
        commands = " ".join(
            str(hook.get("command", ""))
            for entry in _repo_hooks_document()["hooks"].get("PreToolUse", [])
            for hook in entry.get("hooks", [])
        )
        assert [module for module in self._REQUIRED_PRE_TOOL_USE_MODULES if module not in commands] == []

    def test_every_hook_entry_has_a_timeout(self) -> None:
        """全イベントの全 hook エントリが timeout を持つこと。"""
        missing_timeout = [
            (event, hook.get("command"))
            for event, entries in _repo_hooks_document()["hooks"].items()
            for entry in entries
            for hook in entry.get("hooks", [])
            if "timeout" not in hook
        ]
        assert missing_timeout == []

    def test_modules_propagate_exit_code_via_system_exit(self) -> None:
        """hooks.json が起動する全モジュールが実在し、`main()` の戻り値を SystemExit で伝えること。

        `launcher.py` の `_run_module_in_process` は `SystemExit` を捕まえてその
        `code` を返し、例外が上がらなければ `0` を返す。したがって `__main__` が
        `main()` を素で呼ぶモジュールでは、`main()` が 2（deny）を返しても launcher は
        0（allow）を返す — PreToolUse の「exit 2 = deny」契約が黙って壊れる。
        docs/adr/definition-and-subagent-design.md 決定6 / docs/adr/staleness-detection.md が対象とする
        「宣言はあるが CI で実測されない契約」の検知器。
        """
        for argv in _repo_hook_argv():
            module = argv[0]
            spec = importlib.util.find_spec(module)
            assert spec is not None and spec.origin, f"モジュールが存在しません: {module}"
            tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
            main_blocks = [
                node for node in tree.body if isinstance(node, ast.If) and "__main__" in ast.dump(node.test)
            ]
            assert main_blocks, f"{module} に __main__ ブロックがありません"
            source = "\n".join(ast.unparse(stmt) for block in main_blocks for stmt in block.body)
            assert "SystemExit(main())" in source or "sys.exit(main())" in source, (
                f"{module} の __main__ が main() の戻り値を SystemExit で伝えていません: {source!r}"
            )

    def test_mem_cli_hooks_name_an_existing_subcommand(self) -> None:
        """claq.mem.cli 呼び出しが launcher 直後にモジュール名・実在するサブコマンドの順で並ぶこと（``--bg`` 付きも許容）。"""
        from claq.mem.cli import _COMMAND_HANDLERS

        mem_cli_argv = [argv for argv in _repo_hook_argv() if argv[0] == "claq.mem.cli"]
        assert mem_cli_argv
        for argv in mem_cli_argv:
            assert argv[1] in _COMMAND_HANDLERS, f"未定義のサブコマンド: {argv[1]}"


class TestRepoHooksJsonPowerShellParity:
    """実 hooks.json の POSIX / PowerShell 起動行の対応を検証する。

    CLAUDE.md は hooks.json の全エントリが `runtime/claq-hook` を起動し、裸の `python3` を
    呼ばないと規定する。Windows ホストは `powershell` を優先し、無ければ `command` を
    そのまま cmd/PowerShell へ渡そうとして落ちる。したがって「`powershell` が在ること」
    と「POSIX 側と同じモジュール・同じ引数を起動すること」は別々の契約であり、
    後者が崩れると Windows だけ別のフックが走る（あるいは何も走らない）。
    """

    _POSIX_LAUNCHER = "${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook"
    _WINDOWS_LAUNCHER = "${CLAUDE_PLUGIN_ROOT}/runtime/claq-hook.cmd"

    def test_every_command_hook_declares_a_powershell_sibling(self) -> None:
        """command フックが 1 つ以上あり、全てが `powershell` を持つこと。"""
        command_hooks = _repo_command_hooks()
        assert command_hooks, "hooks.json に command フックがありません"
        missing = [event for event, hook in command_hooks if "powershell" not in hook]
        assert missing == [], f"powershell が無い command フック: {missing}"

    def test_every_powershell_line_invokes_the_windows_wrapper(self) -> None:
        """`powershell` が call 演算子で `runtime/claq-hook.cmd` を起動すること。

        PowerShell では引用符付きパスを裸で書くと文字列リテラルとして評価され、
        何も実行されずに成功扱いになる。先頭の `&` はその静かな失敗の検知器。
        """
        for event, hook in _repo_command_hooks():
            parts = shlex.split(hook["powershell"])
            assert parts[0] == "&", f"{event}: call 演算子がありません: {hook['powershell']!r}"
            assert parts[1] == self._WINDOWS_LAUNCHER, f"{event}: wrapper 以外を起動しています: {parts[1]!r}"

    def test_powershell_and_command_launch_the_same_module_and_args(self) -> None:
        """`command` が `runtime/claq-hook` を起動し、`powershell` と同一のモジュール・引数を渡すこと。

        `--bg` のようなランチャーフラグも含めて完全一致を求める。片側にだけ
        付いていると、SessionEnd の handoff が Windows でだけブロッキングに
        なるといった OS 依存の挙動差が無診断で入り込む。
        """
        for event, hook in _repo_command_hooks():
            posix_parts = shlex.split(hook["command"])
            windows_parts = shlex.split(hook["powershell"])
            assert posix_parts[0] == self._POSIX_LAUNCHER, f"{event}: wrapper 以外を起動しています: {posix_parts[0]!r}"
            assert windows_parts[2:] == posix_parts[1:], (
                f"{event}: 引数が一致しません: powershell={windows_parts[2:]} command={posix_parts[1:]}"
            )

    def test_windows_wrapper_file_exists(self) -> None:
        """`powershell` が起動する `runtime/claq-hook.cmd` が実在すること。"""
        assert (REPO_ROOT / "runtime" / "claq-hook.cmd").is_file()
