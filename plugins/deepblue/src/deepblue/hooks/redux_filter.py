"""Bash ツール出力に redux コマンド別圧縮を適用する PostToolUse フック。

``tool_input.command`` でコマンド種別を判定し、対応する redux フィルタで
``tool_response`` を圧縮する。圧縮後 JSON を stdout に出力して tool_response を
上書きする。圧縮効果がない場合やエラー時は raw をそのまま返し、非破壊的
フォールバックを保証する。
"""

from __future__ import annotations

import json
import sys

from deepblue.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout
from deepblue.mem.settings import ReduxSettings, Settings
from deepblue.redux.config import ReduxConfig
from deepblue.redux.engine import ReduxEngine

_ENGINE: ReduxEngine | None = None


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
    """Settings.load() から ReduxConfig を読み込む。失敗時はデフォルト設定を返す。"""
    try:
        settings = Settings.load()
        return _to_redux_config(settings.redux)
    except Exception as e:
        write_stderr(f"[redux] settings load failed: {e}\n")
        return ReduxConfig()


def _get_engine() -> ReduxEngine:
    """組込・ユーザーフィルタを読み込んだエンジンをプロセス内でキャッシュして返す。"""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = ReduxEngine.load()
    return _ENGINE


def _apply_reduction(command: str, original_text: str, data: dict, config: ReduxConfig, engine: ReduxEngine) -> str:
    """redux 圧縮を適用し、削減後の JSON 文字列を返す。削減効果なしの場合は空文字列を返す。

    Args:
        command: 実行された Bash コマンド文字列。
        original_text: 圧縮前の tool_response テキスト。
        data: 元の入力データ辞書。
        config: 圧縮設定。
        engine: フィルタ適用エンジン。

    Returns:
        圧縮効果があれば更新済み JSON 文字列、なければ空文字列。
    """
    try:
        reduced = engine.reduce(command, original_text, config)
    except Exception as e:
        write_stderr(f"[redux] reduction failed: {e}\n")
        return ""
    if len(reduced) >= len(original_text):
        return ""
    saved_pct = (len(original_text) - len(reduced)) / len(original_text) * 100
    write_stderr(f"[redux] {len(original_text)} → {len(reduced)} chars ({saved_pct:.0f}% 削減)\n")
    output_data = dict(data)
    output_data["tool_response"] = reduced
    return json.dumps(output_data, ensure_ascii=False)


def evaluate(raw_input: str, config: ReduxConfig | None = None, engine: ReduxEngine | None = None) -> str:
    """Bash ツール出力を redux で圧縮して返す。

    Args:
        raw_input: フックに渡された生の入力 JSON 文字列。
        config: 圧縮設定。None の場合は Settings.load() から読み込む。
        engine: フィルタ適用エンジン。None の場合はキャッシュ済みエンジンを使う。

    Returns:
        圧縮後の JSON 文字列、または元の raw_input（変更なしの場合）。
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

    command = str((data.get("tool_input") or {}).get("command") or "")
    if engine is None:
        engine = _get_engine()

    result = _apply_reduction(command, original_text, data, config, engine)
    return result if result else raw_input


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
        output = raw
    write_stdout(output)
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
