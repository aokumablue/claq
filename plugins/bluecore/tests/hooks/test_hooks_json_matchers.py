"""hooks.json の matcher と `_TOOL_NAME_MAP` の同期を機械的に担保するテスト。

hooks.json はコメントを書けない JSON なので、matcher の設計判断はここに残す。

matcher は Claude Code / Copilot CLI ともに**正規表現**として評価される
（Copilot CLI 1.0.75 の実セッションログで、matcher
``Edit|Write|MultiEdit|apply_patch`` がツール名 ``apply_patch`` にマッチした
事実から確定）。したがって非アンカーの裸トークンは部分一致し、意図しない
ツール名まで拾う:

- ``write`` は MCP ツール ``mcp__fs__write_file`` に部分一致する
- ``task`` は MCP ツール ``mcp__linear__create_task`` に部分一致する
- ``Edit`` は ``NotebookEdit`` に、``Bash`` は ``BashOutput`` に部分一致する

いずれもフック内のツール名ガード（`config_protection._WRITE_TOOL_NAMES` /
`quality_gate._WRITE_TOOL_NAMES` / `redux_filter` の "Bash" 比較）で最終的には
no-op になるが、その手前で Python プロセスが 1 つ起動する分だけ無駄になる。
そのため全ての tool-gated matcher を ``^(...)$`` でアンカーする。

アンカー化で落ちる名前と、その根拠:

- ``NotebookEdit``: `normalize_tool_name` は "NotebookEdit" を "NotebookEdit" の
  ままにする（`_TOOL_NAME_MAP` の "notebookedit" → "NotebookEdit"）。
  config_protection / quality_gate の `_WRITE_TOOL_NAMES` は
  {edit, write, multiedit} で "notebookedit" を含まないため、従来も必ず
  no-op だった。よって matcher から落として挙動は変わらない。
- ``BashOutput``: `normalize_tool_name("BashOutput")` は "Bash" にならず
  "BashOutput" のままなので redux_filter の "Bash" 比較を通らない。
  block_no_verify / pre_bash_commit_quality は ``tool_input.command`` を読むが
  BashOutput の tool_input に command は無いため空文字列となり no-op。
  よって落として挙動は変わらない。
- ``TaskStop``: pre_agent_nudge は ``subagent_type`` / ``agent_type`` を見るため
  no-op。よって落として挙動は変わらない。

``"*"`` matcher は Claude Code / Copilot CLI ともに正規表現ではなく
「全ツール」を表す特別値として扱われるため、アンカー化の対象外とする。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from bluecore.ci.ci_common import REPO_ROOT
from bluecore.lib.harness import _TOOL_NAME_MAP, normalize_tool_name

HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

# 全ツールを表す特別値。正規表現として評価されないためアンカー化しない。
WILDCARD_MATCHER = "*"

# フック実装モジュール → そのフックが内部で受理する正規化済みツール名。
# ここが matcher と実装の対応表であり、両者のズレを本テストが検出する。
TOOL_GATED_HOOKS: dict[str, frozenset[str]] = {
    # tool_input.command を読む（command が無いツールでは no-op）
    "bluecore.hooks.block_no_verify": frozenset({"Bash"}),
    "bluecore.hooks.pre_bash_commit_quality": frozenset({"Bash"}),
    # normalize_tool_name(...) == "Bash" のみ処理する
    "bluecore.hooks.redux_filter": frozenset({"Bash"}),
    # _WRITE_TOOL_NAMES == {edit, write, multiedit}
    "bluecore.hooks.config_protection": frozenset({"Edit", "Write", "MultiEdit"}),
    "bluecore.hooks.quality_gate": frozenset({"Edit", "Write", "MultiEdit"}),
    # tool_input の subagent_type / agent_type を読むサブエージェント起動フック
    "bluecore.hooks.pre_agent_nudge": frozenset({"Agent"}),
}

ANCHORED_MATCHER_RE = re.compile(r"^\^\((?P<alternatives>[^()]+)\)\$$")


def _load_hooks() -> dict[str, list[dict]]:
    """hooks.json の hooks セクションを読み込む。"""
    return json.loads(Path(HOOKS_JSON).read_text(encoding="utf-8"))["hooks"]


def _iter_entries() -> list[tuple[str, dict]]:
    """(イベント名, matcher エントリ) のリストを返す。"""
    return [(event, entry) for event, entries in _load_hooks().items() for entry in entries]


def _entry_modules(entry: dict) -> set[str]:
    """matcher エントリが起動するフックモジュール名の集合を返す。"""
    commands = " ".join(str(hook.get("command", "")) for hook in entry.get("hooks", []))
    return {module for module in TOOL_GATED_HOOKS if module in commands}


def _tool_gated_entries() -> list[tuple[str, str, frozenset[str]]]:
    """(モジュール名, matcher 文字列, 受理する正規化ツール名) のリストを返す。"""
    found: list[tuple[str, str, frozenset[str]]] = []
    for _event, entry in _iter_entries():
        for module in _entry_modules(entry):
            found.append((module, str(entry["matcher"]), TOOL_GATED_HOOKS[module]))
    return found


def _alternatives(matcher: str) -> list[str]:
    """アンカー済み matcher から選択肢トークンを取り出す。"""
    match = ANCHORED_MATCHER_RE.match(matcher)
    assert match is not None, f"matcher がアンカー形 ^(...)$ ではありません: {matcher}"
    return match.group("alternatives").split("|")


class TestMatcherAnchoring:
    """hooks.json の matcher がアンカー済みであることの検証。"""

    def test_all_non_wildcard_matchers_are_anchored(self) -> None:
        """"*" 以外の全 matcher は ^(...)$ でアンカーされている。"""
        unanchored = [
            (event, entry["matcher"])
            for event, entry in _iter_entries()
            if entry.get("matcher") != WILDCARD_MATCHER and not ANCHORED_MATCHER_RE.match(str(entry.get("matcher", "")))
        ]
        assert unanchored == []

    def test_every_tool_gated_hook_is_present(self) -> None:
        """対応表の全フックが hooks.json に登録されている（表の腐敗検出）。"""
        assert {module for module, _matcher, _accepted in _tool_gated_entries()} == set(TOOL_GATED_HOOKS)


class TestMatcherToolNameMapSync:
    """matcher と `_TOOL_NAME_MAP` の手動同期をテストで担保する。"""

    @pytest.mark.parametrize(("module", "matcher", "accepted"), _tool_gated_entries())
    def test_matcher_covers_every_alias_of_accepted_tools(
        self, module: str, matcher: str, accepted: frozenset[str]
    ) -> None:
        """フックが受理する正規化名に写る `_TOOL_NAME_MAP` のキーが matcher に揃っている。"""
        alternatives = set(_alternatives(matcher))
        # 正規化前の別名（Copilot CLI の小文字名・Codex の apply_patch 等）
        aliases = {raw for raw, normalized in _TOOL_NAME_MAP.items() if normalized in accepted}
        # 正規化後の名前（Claude Code 表記）そのものも matcher に必要
        missing = (aliases | set(accepted)) - alternatives
        assert missing == set(), f"{module}: matcher に不足しているツール名 {sorted(missing)}"

    @pytest.mark.parametrize(("module", "matcher", "accepted"), _tool_gated_entries())
    def test_matcher_has_no_tool_name_the_hook_ignores(
        self, module: str, matcher: str, accepted: frozenset[str]
    ) -> None:
        """matcher に、フックが内部で必ず無視する（= 起動が無駄な）ツール名が無い。"""
        stray = {name for name in _alternatives(matcher) if normalize_tool_name(name) not in accepted}
        assert stray == set(), f"{module}: フックが受理しないツール名が matcher に残っています {sorted(stray)}"


# Grok Build TUI 対応ラッパー
#
# Grok Build TUI は hooks.json の command 文字列内 ``${CLAUDE_PLUGIN_ROOT}`` を
# 常に ``~/.grok/plugins/bluecore`` に展開するが、`grok plugin install` の実体は
# ``~/.grok/installed-plugins/bluecore-<hash>/`` に置かれる（hash は更新の
# たびに変わる）。symlink（`ensure_grok_plugin_root_symlink()`）は
# launcher.py が一度起動できて初めて SessionStart 経由で張られるため、
# symlink が無い初回はこの経路そのものに到達できない（鶏卵問題）。
#
# そこで command 文字列を、launcher.py の所在を実行時に解決するシェル
# ラッパーへ置き換える:
#   1. ``$CLAUDE_PLUGIN_ROOT/src/bluecore/launcher.py`` があればそれを使う
#      （Claude Code 経路。この 1 行で確定し挙動は不変）
#   2. 無ければ ``~/.grok/installed-plugins/bluecore-*/src/bluecore/launcher.py``
#      のうち mtime 最新のものを使う（Grok 経路）
#   3. どちらも無ければ ``exit 0``（``exit 2`` は偽 deny を再現するので禁止）
#   4. 見つかった launcher を ``exec python3 "$L" <元の引数>`` で実行する。
#      ``exec`` はサブシェル経由で終了コードを握りつぶさないために必須。
#      block_no_verify / config_protection 等は exit 2 でブロックする契約の
#      ため、この exec が崩れると Claude Code の全ブロック系フックが無効化
#      される。

# 旧来の直接呼び出し形（Claude Code 専用、Grok では鶏卵問題で到達不能）。
# 全 13 エントリからこの形が消えていることを形状検証で担保する。
_RAW_LAUNCHER_CALL_RE = re.compile(r'^python3\s+"\$\{CLAUDE_PLUGIN_ROOT\}/src/bluecore/launcher\.py"')

# 新ラッパー形の目印。fallback で installed-plugins glob を参照し、
# 最終手段は必ず python3 launcher.py を exec すること。
_WRAPPER_MARKERS = (
    'L="${CLAUDE_PLUGIN_ROOT}/src/bluecore/launcher.py"',
    ".grok/installed-plugins/bluecore-*/src/bluecore/launcher.py",
    "exec python3 \"$L\"",
)


def _all_commands() -> list[str]:
    """hooks.json の全エントリから command 文字列を平坦なリストで返す。"""
    return [
        str(hook.get("command", ""))
        for _event, entry in _iter_entries()
        for hook in entry.get("hooks", [])
    ]


class TestGrokWrapperShape:
    """全 13 エントリの command が Grok 対応ラッパー形であることの形状検証。"""

    def test_no_raw_launcher_invocation_remains(self) -> None:
        """旧来の直接呼び出し（Grok 経路で鶏卵問題を起こす形）が1件も残っていない。"""
        raw = [cmd for cmd in _all_commands() if _RAW_LAUNCHER_CALL_RE.match(cmd)]
        assert raw == []

    def test_every_command_is_wrapper_shaped(self) -> None:
        """全 command がラッパーの3要素（Claude 直参照 / Grok fallback / exec）を含む。"""
        commands = _all_commands()
        assert len(commands) == 13
        for command in commands:
            missing = [marker for marker in _WRAPPER_MARKERS if marker not in command]
            assert missing == [], f"ラッパー形になっていません: {command!r} (欠落: {missing})"

    def test_no_wrapper_swallows_exit_code_via_subshell(self) -> None:
        """`exec python3 "$L" ...` の後ろに追加の shell 文が無い（終了コードを握りつぶさない）。"""
        exec_marker = 'exec python3 "$L"'
        for command in _all_commands():
            tail = command[command.index(exec_marker) + len(exec_marker) :]
            assert ";" not in tail, f"exec の後に追加の shell 文があります（終了コードを隠す可能性）: {command!r}"


def _write_stub_launcher(path: Path, *, exit_code: int, echo_argv: bool = True) -> None:
    """テスト用のダミー launcher.py を書き込む。

    Args:
        path: 書き込み先の launcher.py パス（親ディレクトリは自動作成）。
        exit_code: ダミー launcher が終了するコード。
        echo_argv: True なら受け取った sys.argv[1:] を JSON として stdout に出す。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["import sys", "import json", 'print("stub-ran")']
    if echo_argv:
        lines.append("print(json.dumps(sys.argv[1:]))")
    lines.append(f"sys.exit({exit_code})")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_wrapper(
    command: str,
    *,
    home: Path,
    claude_plugin_root: str | None,
) -> subprocess.CompletedProcess[str]:
    """hooks.json 由来の1 command 文字列を隔離環境で `sh -c` 実行する。

    Args:
        command: hooks.json の command 文字列（そのまま `sh -c` に渡す）。
        home: 偽の $HOME（実 ~/.grok には一切触れない）。
        claude_plugin_root: $CLAUDE_PLUGIN_ROOT に設定する値。None なら未設定。

    Returns:
        CompletedProcess（stdin は空文字列を渡す）。
    """
    # 実 PATH をそのまま渡す。固定パスにすると、実行中の python3 がその外に
    # あるだけで `sh -c` が 127 を返し、exit 2 伝播ガードが無関係な理由で
    # 落ちる（このマシンの python3 は /opt/homebrew 配下で /usr/bin 外）。
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "")}
    if claude_plugin_root is not None:
        env["CLAUDE_PLUGIN_ROOT"] = claude_plugin_root
    return subprocess.run(
        ["sh", "-c", command],
        input="",
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        check=False,
    )


# 実行検証の対象は block_no_verify エントリ1本で十分（全エントリ共通のラッパー
# ロジックを検証しており、テンプレートは _WRAPPER_MARKERS で全エントリ共通と
# 既に確認済み）。--bg 引数順の検証だけは4エントリ全てに対して行う。
def _block_no_verify_command() -> str:
    """block_no_verify エントリの command 文字列を hooks.json から取得する。"""
    for _event, entry in _iter_entries():
        for hook in entry.get("hooks", []):
            if "bluecore.hooks.block_no_verify" in str(hook.get("command", "")):
                return str(hook["command"])
    raise AssertionError("block_no_verify エントリが hooks.json に見つかりません")


def _bg_commands() -> list[str]:
    """`--bg` を含む4エントリの command 文字列を返す。"""
    return [cmd for cmd in _all_commands() if "--bg" in cmd]


class TestGrokWrapperExecution:
    """ラッパーの解決ロジック（Claude優先 / Grok fallback / exit 0 / exit 2伝播）の実行検証。"""

    def test_grok_fallback_runs_when_claude_plugin_root_missing(self, tmp_path: Path) -> None:
        """CLAUDE_PLUGIN_ROOT が無くても ~/.grok/installed-plugins/bluecore-* の launcher が実行される。"""
        home = tmp_path / "home"
        stub = home / ".grok" / "installed-plugins" / "bluecore-abc" / "src" / "bluecore" / "launcher.py"
        _write_stub_launcher(stub, exit_code=0)

        result = _run_wrapper(_block_no_verify_command(), home=home, claude_plugin_root=None)

        assert "stub-ran" in result.stdout
        assert result.returncode == 0

    def test_claude_plugin_root_takes_priority_over_grok_fallback(self, tmp_path: Path) -> None:
        """CLAUDE_PLUGIN_ROOT の launcher が存在すれば installed-plugins glob は参照されない。"""
        home = tmp_path / "home"
        grok_stub = home / ".grok" / "installed-plugins" / "bluecore-xyz" / "src" / "bluecore" / "launcher.py"
        grok_stub.parent.mkdir(parents=True, exist_ok=True)
        grok_stub.write_text('print("WRONG-grok-ran")\n', encoding="utf-8")

        claude_root = tmp_path / "claude-plugin-root"
        claude_launcher = claude_root / "src" / "bluecore" / "launcher.py"
        _write_stub_launcher(claude_launcher, exit_code=0)

        result = _run_wrapper(
            _block_no_verify_command(), home=home, claude_plugin_root=str(claude_root)
        )

        assert "stub-ran" in result.stdout
        assert "WRONG-grok-ran" not in result.stdout
        assert result.returncode == 0

    def test_neither_path_found_exits_zero_without_running_anything(self, tmp_path: Path) -> None:
        """CLAUDE_PLUGIN_ROOT 未設定かつ ~/.grok/installed-plugins/ にも何も無ければ exit 0。"""
        home = tmp_path / "home"
        home.mkdir(parents=True, exist_ok=True)

        result = _run_wrapper(_block_no_verify_command(), home=home, claude_plugin_root=None)

        assert result.returncode == 0
        assert result.stdout == ""

    def test_exit_code_2_propagates_through_wrapper(self, tmp_path: Path) -> None:
        """launcher が exit 2 すれば、ラッパー経由でも exit 2 が返る（ブロック契約の最重要ガード）。"""
        home = tmp_path / "home"
        stub = home / ".grok" / "installed-plugins" / "bluecore-abc" / "src" / "bluecore" / "launcher.py"
        _write_stub_launcher(stub, exit_code=2)

        result = _run_wrapper(_block_no_verify_command(), home=home, claude_plugin_root=None)

        assert result.returncode == 2

    @pytest.mark.parametrize("command", _bg_commands())
    def test_bg_flag_position_preserved_for_async_entries(self, tmp_path: Path, command: str) -> None:
        """--bg エントリで、`--bg` がモジュール名より前の位置関係を保ったまま渡される。"""
        home = tmp_path / "home"
        stub = home / ".grok" / "installed-plugins" / "bluecore-abc" / "src" / "bluecore" / "launcher.py"
        _write_stub_launcher(stub, exit_code=0, echo_argv=True)

        result = _run_wrapper(command, home=home, claude_plugin_root=None)

        argv_line = result.stdout.splitlines()[-1]
        received_args = json.loads(argv_line)
        assert received_args[0] == "--bg"
        assert not received_args[1].startswith("--")
