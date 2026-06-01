"""Bash ツール出力にトークン削減を適用する PostToolUse フック。

stdout に JSON を出力することで tool_response を上書きする。
削減効果がない場合やエラー時は raw をそのまま返し、非破壊的フォールバックを保証する。
"""

from __future__ import annotations

import json
import sys

from deepblue.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout
from deepblue.mem.reducer import ReduceConfig, reduce_bash_output
from deepblue.mem.settings import CompactSettings, Settings


def _to_reduce_config(compact: CompactSettings) -> ReduceConfig:
    """CompactSettings を ReduceConfig に変換する。"""
    return ReduceConfig(
        enabled=compact.enabled,
        smart_filter_enabled=compact.smart_filter_enabled,
        group_lint_enabled=compact.group_lint_enabled,
        dedup_enabled=compact.dedup_enabled,
        smart_truncate_enabled=compact.smart_truncate_enabled,
        max_output_len=compact.max_output_len,
        head_lines=compact.head_lines,
        tail_lines=compact.tail_lines,
        dedup_threshold=compact.dedup_threshold,
    )


def _load_config() -> ReduceConfig:
    """Settings.load() から ReduceConfig を読み込む。失敗時はデフォルト設定を返す。"""
    try:
        settings = Settings.load()
        return _to_reduce_config(settings.compact)
    except Exception as e:
        write_stderr(f"[Compact] settings load failed: {e}\n")
        return ReduceConfig()


def _apply_reduction(original_text: str, data: dict, config: ReduceConfig) -> str:
    """テキスト削減を適用し、削減後の JSON 文字列を返す。削減効果なしの場合は空文字列を返す。

    Args:
        original_text: 削減前の Bash ツール出力テキスト。
        data: 元の入力データ辞書。
        config: 削減設定。

    Returns:
        削減効果があれば更新済み JSON 文字列、なければ空文字列。
    """
    try:
        reduced = reduce_bash_output(original_text, config)
    except Exception as e:
        write_stderr(f"[Compact] reduction failed: {e}\n")
        return ""
    if len(reduced) >= len(original_text):
        return ""
    saved_pct = (len(original_text) - len(reduced)) / len(original_text) * 100
    write_stderr(f"[Compact] {len(original_text)} → {len(reduced)} chars ({saved_pct:.0f}% 削減)\n")
    output_data = dict(data)
    output_data["tool_response"] = reduced
    return json.dumps(output_data, ensure_ascii=False)


def evaluate(raw_input: str, config: ReduceConfig | None = None) -> str:
    """Bash ツール出力を削減して返す。

    Args:
        raw_input: フックに渡された生の入力 JSON 文字列。
        config: 削減設定。None の場合は Settings.load() から読み込む。

    Returns:
        削減後の JSON 文字列、または元の raw_input（変更なしの場合）。
    """
    data = parse_json_object(raw_input)
    if data is None:
        return raw_input
    if str(data.get("tool_name", "") or "") != "Bash":
        return raw_input
    tool_response = data.get("tool_response")
    if not tool_response:
        return raw_input
    original_text = str(tool_response)
    if not original_text.strip():
        return raw_input

    if config is None:
        config = _load_config()
    if not config.enabled:
        return raw_input

    result = _apply_reduction(original_text, data, config)
    return result if result else raw_input


def main() -> int:
    """Bash 出力のトークン削減フックのエントリポイント。

    Returns:
        終了コード（0: 常に成功）。
    """
    raw = read_raw_stdin()
    try:
        output = evaluate(raw)
    except Exception as e:
        write_stderr(f"[Compact] unexpected error: {e}\n")
        output = raw
    write_stdout(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
