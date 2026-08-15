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
import re
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
