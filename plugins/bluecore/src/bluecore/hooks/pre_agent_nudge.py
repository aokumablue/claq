"""general-purpose / Explore サブエージェント起動時に専門エージェント対応表を提示します。

トリガー: PreToolUse (Task|Agent)
入力: subagent_type を含む tool_input JSON
出力:
    - subagent_type == "general-purpose" のとき hookSpecificOutput に AGENT_TABLE を注入
    - subagent_type == "Explore" のとき hookSpecificOutput に EXPLORE_TABLE を注入
終了: 0（いかなる場合もブロックしない）

上記 2 種以外の subagent_type、tool_input 欠落・非 dict 時は無出力で 0 を返す。
"""

from __future__ import annotations

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stdout
from bluecore.hooks.output_adapter import adapt_pre_tool_use_context_output

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


def main() -> int:
    """general-purpose / Explore サブエージェント起動を検知して対応表を提示する。

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

    tool_input = data.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    subagent_type = str(tool_input.get("subagent_type") or "")
    if subagent_type == "general-purpose":
        write_stdout(adapt_pre_tool_use_context_output(AGENT_TABLE))
        return 0
    if subagent_type == "Explore":
        write_stdout(adapt_pre_tool_use_context_output(EXPLORE_TABLE))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
