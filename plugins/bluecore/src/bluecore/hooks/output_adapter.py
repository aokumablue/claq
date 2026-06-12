"""フック出力をハーネス別プロトコルへ変換するアダプタ。

Claude Code / unknown では変換ゼロ（現行の hookSpecificOutput 形式と exit code 2
ブロックをそのまま使う）。Copilot CLI / Codex では各ハーネスのプロトコル差を
ここで一元的に吸収する。変換はデータ駆動のマッピングテーブルで定義し、
実機検証の結果に応じてテーブルのエントリ差し替えのみで対応できる構造とする。
"""

from __future__ import annotations

import json

from bluecore.lib.harness import detect_harness


def _claude_context_output(event_name: str, additional_context: str) -> str:
    """Claude Code の hookSpecificOutput 形式 JSON を返す。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        hookSpecificOutput を含む JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            }
        },
        ensure_ascii=False,
    )


# ハーネス → コンテキスト注入出力ビルダーのマッピング。
# Codex / Copilot は PascalCase イベント + snake_case stdin の Claude 互換形式を
# サポートするため、まず Claude 形式で出力する（実機検証で解釈されないと
# 判明した場合はここのエントリだけを差し替える）。
_CONTEXT_OUTPUT_BUILDERS = {
    "claude": _claude_context_output,
    "codex": _claude_context_output,
    "copilot": _claude_context_output,
    "unknown": _claude_context_output,
}


def adapt_context_output(event_name: str, additional_context: str) -> str:
    """コンテキスト注入出力を実行中ハーネスのプロトコルに合わせて生成する。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネスのプロトコルに適合した JSON 文字列。

    Raises:
        例外は発生しません。
    """
    # 将来のハーネス追加でテーブル更新が漏れても KeyError にせず Claude 形式へ倒す
    builder = _CONTEXT_OUTPUT_BUILDERS.get(detect_harness(), _claude_context_output)
    return builder(event_name, additional_context)


def emit_block(reason: str) -> tuple[int, str, str]:
    """ツール実行ブロックの出力をハーネス別に組み立てる。

    Claude Code / Codex は exit code 2 + stderr でブロックする。Copilot CLI は
    exit code 2 が warning（fail-open）のため、permissionDecision: deny の
    stdout JSON + exit code 0 へ変換する。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        (exit_code, stdout, stderr) のタプル。呼び出し側はこの 3 つを
        そのまま出力・終了コードに使うこと。

    Raises:
        例外は発生しません。
    """
    if detect_harness() == "copilot":
        payload = json.dumps(
            {"permissionDecision": "deny", "permissionDecisionReason": reason},
            ensure_ascii=False,
        )
        return 0, payload, ""
    return 2, "", reason
