"""Bash ツール出力に redux コマンド別圧縮を適用する PostToolUse フック。

``tool_input.command`` でコマンド種別を判定し、対応する redux フィルタで
ツール出力（``tool_response.stdout``）を圧縮する。圧縮できた場合のみ
Claude Code の ``hookSpecificOutput.updatedToolOutput`` 契約で更新後の
ツール出力を返し、stdout を上書きする。``updatedToolOutput`` は元の
ツール出力と同じ形（stdout/stderr/interrupted/isImage…）を保つ必要があり、
形が合わないと Claude Code 側で破棄され元出力が使われる。圧縮効果がない・
対象外・エラー時は何も出力せず、元のツール出力をそのまま通す。
"""

from __future__ import annotations

import json
import sys

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout
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


def _apply_reduction(
    command: str,
    original_stdout: str,
    tool_response: dict,
    config: ReduxConfig,
    engine: ReduxEngine,
) -> str:
    """redux 圧縮を適用し、``updatedToolOutput`` 契約の JSON 文字列を返す。

    Args:
        command: 実行された Bash コマンド文字列。
        original_stdout: 圧縮前の ``tool_response.stdout`` テキスト。
        tool_response: 元のツール出力オブジェクト。stdout 以外のキーを保持して
            output shape を維持するために使う。
        config: 圧縮設定。
        engine: フィルタ適用エンジン。

    Returns:
        圧縮効果があれば ``updatedToolOutput`` を含む JSON 文字列、なければ空文字列。
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
    updated_output = {**tool_response, "stdout": reduced}
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "updatedToolOutput": updated_output,
        }
    }
    return json.dumps(output, ensure_ascii=False)


def evaluate(raw_input: str, config: ReduxConfig | None = None, engine: ReduxEngine | None = None) -> str:
    """Bash ツール出力を redux で圧縮し、``updatedToolOutput`` 契約の JSON を返す。

    Args:
        raw_input: フックに渡された生の入力 JSON 文字列。
        config: 圧縮設定。None の場合は Settings.load() から読み込む。
        engine: フィルタ適用エンジン。None の場合はキャッシュ済みエンジンを使う。

    Returns:
        圧縮できた場合は ``updatedToolOutput`` を含む JSON 文字列。
        対象外・無効・圧縮効果なし・エラー時は空文字列（フックは出力せず透過する）。
    """
    data = parse_json_object(raw_input)
    if data is None:
        return ""
    if str(data.get("tool_name", "") or "") != "Bash":
        return ""
    tool_response = data.get("tool_response")
    if not isinstance(tool_response, dict):
        return ""
    stdout = tool_response.get("stdout")
    if not isinstance(stdout, str) or not stdout.strip():
        return ""

    if config is None:
        config = _load_config()
    if not config.enabled:
        return ""

    command = str((data.get("tool_input") or {}).get("command") or "")[:_MAX_COMMAND_LEN]
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
