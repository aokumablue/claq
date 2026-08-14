"""general-purpose / Explore エージェント起動時に専門エージェント対応表を提示します。

トリガー: PreToolUse (Task|Agent)
入力: extract_tool_input() が返す dict 内の subagent_type または agent_type
出力:
    - Grok（detect_harness() == "grok"）上で 種別 が explore（小文字）/
      general-purpose / plan のいずれかのとき hookSpecificOutput に
      GROK_MESSAGE を注入する。Grok の spawn_subagent が受理する型は
      explore/general-purpose/plan の 3 つのみで bluecore 専門エージェント型は
      存在しない（実機で "Unknown subagent type" エラーを確認済み）ため、Claude 用
      AGENT_TABLE/EXPLORE_TABLE とは異なる文面を使う。
    - それ以外のハーネスでは種別 == "general-purpose" のとき AGENT_TABLE、
      種別 == "Explore" のとき EXPLORE_TABLE を注入する（Claude Code の
      挙動は本分岐追加前と完全に同一）。
終了: 0（いかなる場合もブロックしない）

同一 dict 内では subagent_type を優先し、Copilot の agent_type はフォールバック
として扱う。上記に該当しない種別、入力欠落・非 dict 時は無出力で 0 を返す。
"""

from __future__ import annotations

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stdout
from bluecore.hooks.output_adapter import adapt_pre_tool_use_context_output
from bluecore.lib.harness import detect_harness, extract_tool_input

AGENT_TABLE: str = """\
[bluecore] general-purpose の代わりに専門エージェントが使える場合は subagent_type を差し替えること（該当なしなら general-purpose のままでよい）:
- 編集込み汎用実行（実装+修正+検証を完遂） → bluecore:executor
- コード調査/影響範囲/類似実装 → bluecore:explorer
- 手順分解/依存関係/計画 → bluecore:planner
- 設計判断/アーキテクチャ → bluecore:architect
- テストファースト実装 → bluecore:tdd-writer
- コードレビュー → bluecore:reviewer
- セキュリティ監査 → bluecore:security-auditor
- コード単純化/整理 → bluecore:simplifier
- デッドコード削除 → bluecore:dead-code-cleaner
- 性能分析/最適化 → bluecore:perf-optimizer
- リファクタ統括 → bluecore:refactor-orchestrator"""

EXPLORE_TABLE: str = (
    "[bluecore] 読み取り専用の調査なら bluecore:explorer が使える"
    "（エントリポイント検出/コールチェーン追跡/影響範囲特定/アーキテクチャ把握に特化、"
    "file:line 付き証拠ベース報告）。該当しなければ Explore のままでよい"
)

# subagent_type / agent_type の値 → 注入する対応表（Claude Code / Copilot 用）。
_TABLE_BY_KIND: dict[str, str] = {
    "general-purpose": AGENT_TABLE,
    "Explore": EXPLORE_TABLE,
}

# Grok の spawn_subagent が受理する型（実機で確認済み）。bluecore 専門エージェント型は含まれない。
_GROK_SUBAGENT_TYPES: frozenset[str] = frozenset({"explore", "general-purpose", "plan"})

GROK_MESSAGE: str = (
    "[bluecore] このホストに bluecore 専門エージェント型は無い。explore / plan / general-purpose を使い、"
    "必要なら agents/<name>.md を Read してプロンプト先頭に貼ること。"
)


def main() -> int:
    """general-purpose / Explore エージェント起動を検知して対応表を提示する。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（常に 0 — いかなる場合もブロックしない）。

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    data = parse_json_object(raw)
    if data is None:
        return 0

    tool_input = extract_tool_input(data)
    if not isinstance(tool_input, dict):
        return 0

    subagent_type = str(tool_input.get("subagent_type") or "")
    agent_type = str(tool_input.get("agent_type") or "")
    kind = subagent_type or agent_type

    if detect_harness() == "grok":
        table = GROK_MESSAGE if kind in _GROK_SUBAGENT_TYPES else None
    else:
        table = _TABLE_BY_KIND.get(kind)

    if table:
        write_stdout(adapt_pre_tool_use_context_output(table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
