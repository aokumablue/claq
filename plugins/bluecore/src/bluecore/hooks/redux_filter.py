"""Bash ツール出力に redux コマンド別圧縮を適用する PostToolUse フック。

``tool_input.command`` でコマンド種別を判定し、対応する redux フィルタで
ツール出力（``tool_response.stdout``）を圧縮する。圧縮できた場合のみ
ツール出力の差し替えを返し、stdout を上書きする。ハーネス別の出力契約
（Claude Code の ``updatedToolOutput`` / Copilot CLI の ``modifiedResult``）は
``output_adapter.adapt_tool_output`` に集約してあり、本フックは分岐を持たない。
圧縮効果がない・対象外・エラー時は何も出力せず、元のツール出力をそのまま通す。
"""

from __future__ import annotations

import sys

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout
from bluecore.hooks.output_adapter import adapt_tool_output
from bluecore.lib.harness import extract_bash_command, extract_tool_result_text, normalize_tool_name
from bluecore.mem.settings import ReduxSettings, Settings
from bluecore.redux.config import ReduxConfig
from bluecore.redux.engine import ReduxEngine

_ENGINE: ReduxEngine | None = None
_MAX_COMMAND_LEN = 2000


def _to_redux_config(redux: ReduxSettings) -> ReduxConfig:
    """ReduxSettings を ReduxConfig に変換する。"""
    return ReduxConfig(
        enabled=redux.enabled,
        smart_filter_enabled=redux.smart_filter_enabled,
        group_lint_enabled=redux.group_lint_enabled,
        dedup_enabled=redux.dedup_enabled,
        smart_truncate_enabled=redux.smart_truncate_enabled,
        max_output_len=redux.max_output_len,
        head_lines=redux.head_lines,
        tail_lines=redux.tail_lines,
        dedup_threshold=redux.dedup_threshold,
    )


def _load_config() -> ReduxConfig:
    """Settings から ReduxConfig を読み込む。失敗時はデフォルト設定を返す。"""
    try:
        return _to_redux_config(Settings().redux)
    except Exception as e:
        write_stderr(f"[redux] settings load failed: {e}\n")
        return ReduxConfig()


def _get_engine() -> ReduxEngine:
    """組込・ユーザーフィルタを読み込んだエンジンをプロセス内でキャッシュして返す。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ReduxEngine.load()
    return _ENGINE


def _apply_reduction(
    command: str,
    original_stdout: str,
    tool_response: dict,
    config: ReduxConfig,
    engine: ReduxEngine,
) -> str:
    """redux 圧縮を適用し、ツール出力差し替え契約の JSON 文字列を返す。

    Args:
        command: 実行された Bash コマンド文字列。
        original_stdout: 圧縮前の ``tool_response.stdout`` テキスト。
        tool_response: 元のツール出力オブジェクト。stdout 以外のキーを保持して
            output shape を維持するために使う。
        config: 圧縮設定。
        engine: フィルタ適用エンジン。

    Returns:
        圧縮効果があればハーネス別のツール出力差し替え JSON 文字列、
        なければ空文字列。
    """
    try:
        reduced = engine.reduce(command, original_stdout, config)
    except Exception as e:
        write_stderr(f"[redux] reduction failed: {e}\n")
        return ""
    if len(reduced) >= len(original_stdout):
        return ""
    saved_pct = (len(original_stdout) - len(reduced)) / len(original_stdout) * 100
    write_stderr(f"[redux] {len(original_stdout)} → {len(reduced)} chars ({saved_pct:.0f}% 削減)\n")
    return adapt_tool_output(reduced, tool_response)


def evaluate(raw_input: str, config: ReduxConfig | None = None, engine: ReduxEngine | None = None) -> str:
    """Bash ツール出力を redux で圧縮し、ツール出力差し替え契約の JSON を返す。

    Args:
        raw_input: フックに渡された生の入力 JSON 文字列。
        config: 圧縮設定。None の場合は Settings から読み込む。
        engine: フィルタ適用エンジン。None の場合はキャッシュ済みエンジンを使う。

    Returns:
        圧縮できた場合はハーネス別のツール出力差し替え JSON 文字列。
        対象外・無効・圧縮効果なし・エラー時は空文字列（フックは出力せず透過する）。
    """
    data = parse_json_object(raw_input)
    if data is None:
        return ""
    tool_name = str(data.get("tool_name") or data.get("toolName") or "")
    if normalize_tool_name(tool_name) != "Bash":
        return ""
    stdout, tool_response = extract_tool_result_text(data)
    if not stdout:
        return ""

    if config is None:
        config = _load_config()
    if not config.enabled:
        return ""

    command = extract_bash_command(data)[:_MAX_COMMAND_LEN]
    if engine is None:
        engine = _get_engine()

    return _apply_reduction(command, stdout, tool_response, config, engine)


def main() -> int:
    """Bash 出力の redux 圧縮フックのエントリポイント。

    Returns:
        終了コード（0: 常に成功）。
    """
    raw = read_raw_stdin()
    try:
        output = evaluate(raw)
    except Exception as e:
        write_stderr(f"[redux] unexpected error: {e}\n")
        output = ""
    if output:
        write_stdout(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
