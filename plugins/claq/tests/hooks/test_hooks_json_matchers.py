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
block_no_verify・pre_bash_commit_quality の "Bash" 比較）で最終的には
no-op になるが、その手前で Python プロセスが 1 つ起動する分だけ無駄になる。
そのため全ての tool-gated matcher を ``^(...)$`` でアンカーする。

アンカー化で落ちる名前と、その根拠:

- ``NotebookEdit``: `normalize_tool_name` は "NotebookEdit" を "NotebookEdit" の
  ままにする（`_TOOL_NAME_MAP` の "notebookedit" → "NotebookEdit"）。
  config_protection の `_WRITE_TOOL_NAMES` は {edit, write, multiedit} で
  "notebookedit" を含まないため、必ず no-op になる。よって matcher
  に含めなくても挙動は変わらない。
- ``BashOutput``: `normalize_tool_name("BashOutput")` は "Bash" にならない。
  block_no_verify / pre_bash_commit_quality は ``tool_input.command`` を読むが
  BashOutput の tool_input に command は無いため空文字列となり no-op。
  よって落として挙動は変わらない。

``"*"`` matcher は Claude Code / Copilot CLI ともに正規表現ではなく
「全ツール」を表す特別値として扱われるため、アンカー化の対象外とする。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from claq.ci.ci_common import REPO_ROOT
from claq.lib.harness import _TOOL_NAME_MAP, normalize_tool_name

HOOKS_JSON = REPO_ROOT / "hooks" / "hooks.json"

# 全ツールを表す特別値。正規表現として評価されないためアンカー化しない。
WILDCARD_MATCHER = "*"

# フック実装モジュール → そのフックが内部で受理する正規化済みツール名。
# ここが matcher と実装の対応表であり、両者のズレを本テストが検出する。
TOOL_GATED_HOOKS: dict[str, frozenset[str]] = {
    # tool_input.command を読む（command が無いツールでは no-op）
    "claq.hooks.block_no_verify": frozenset({"Bash"}),
    "claq.hooks.pre_bash_commit_quality": frozenset({"Bash"}),
    # _BASH_TOOL_NAMES == {bash}（正規化後 "Bash"）。config_protection の
    # Edit/Write/MultiEdit 限定を補完する Bash 経由の直接書き換え検知。
    "claq.hooks.bash_config_protection": frozenset({"Bash"}),
    # _WRITE_TOOL_NAMES == {edit, write, multiedit}
    "claq.hooks.config_protection": frozenset({"Edit", "Write", "MultiEdit"}),
}

ANCHORED_MATCHER_RE = re.compile(r"^\^\((?P<alternatives>[^()]+)\)\$$")


def _iter_entries() -> list[tuple[str, dict]]:
    """hooks.json の (イベント名, matcher エントリ) のリストを返す。"""
    hooks = json.loads(Path(HOOKS_JSON).read_text(encoding="utf-8"))["hooks"]
    return [(event, entry) for event, entries in hooks.items() for entry in entries]


def _tool_gated_entries() -> list[tuple[str, str, frozenset[str]]]:
    """(対応表のモジュール名, それを起動するエントリの matcher 文字列, 受理する正規化ツール名) のリストを返す。"""
    return [
        (module, str(entry["matcher"]), accepted)
        for _event, entry in _iter_entries()
        for module, accepted in TOOL_GATED_HOOKS.items()
        if module in " ".join(str(hook.get("command", "")) for hook in entry.get("hooks", []))
    ]


def _alternatives(matcher: str) -> list[str]:
    """アンカー済み matcher から選択肢トークンを取り出す。"""
    match = ANCHORED_MATCHER_RE.match(matcher)
    assert match is not None, f"matcher がアンカー形 ^(...)$ ではありません: {matcher}"
    return match.group("alternatives").split("|")


def test_all_non_wildcard_matchers_are_anchored() -> None:
    """"*" 以外の全 matcher は ^(...)$ でアンカーされている。"""
    unanchored = [
        (event, entry["matcher"])
        for event, entry in _iter_entries()
        if entry.get("matcher") != WILDCARD_MATCHER and not ANCHORED_MATCHER_RE.match(str(entry.get("matcher", "")))
    ]
    assert unanchored == []


def test_every_tool_gated_hook_is_present() -> None:
    """対応表の全フックが hooks.json に登録されている（表の腐敗検出）。"""
    assert {module for module, _matcher, _accepted in _tool_gated_entries()} == set(TOOL_GATED_HOOKS)


@pytest.mark.parametrize(("module", "matcher", "accepted"), _tool_gated_entries())
def test_matcher_lists_every_accepted_alias_and_no_ignored_name(
    module: str, matcher: str, accepted: frozenset[str]
) -> None:
    """matcher と `_TOOL_NAME_MAP` の手動同期: 受理する正規化名とその全別名が揃い、フックが必ず無視する（起動が無駄な）名前が無いこと。"""
    alternatives = set(_alternatives(matcher))
    # 正規化前の別名（Copilot CLI の小文字名・Codex の apply_patch 等）と、正規化後の名前（Claude Code 表記）そのもの。
    aliases = {raw for raw, normalized in _TOOL_NAME_MAP.items() if normalized in accepted}
    missing = (aliases | set(accepted)) - alternatives
    stray = {name for name in alternatives if normalize_tool_name(name) not in accepted}

    assert missing == set(), f"{module}: matcher に不足しているツール名 {sorted(missing)}"
    assert stray == set(), f"{module}: フックが受理しないツール名が matcher に残っています {sorted(stray)}"
