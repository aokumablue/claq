"""フック出力をハーネス別プロトコルへ変換するアダプタ。

Claude Code / unknown では変換ゼロ（現行の hookSpecificOutput 形式と exit code 2
ブロックをそのまま使う）。Copilot CLI / Codex では各ハーネスのプロトコル差を
ここで一元的に吸収する。変換はデータ駆動のマッピングテーブルで定義し、
実機検証の結果に応じてテーブルのエントリ差し替えのみで対応できる構造とする。

Copilot CLI での動作:
  - SessionStart 出力ハンドラ: c?.additionalContext を TOP LEVEL から直接読む
  - UserPromptSubmit 出力ハンドラ: l.additionalContext を TOP LEVEL から直接読む
  - Claude Code 形式の hookSpecificOutput ラッパーは SessionStart/UserPromptSubmit では
    無視される（preToolUse の _vsCodeCompat ブランチのみで参照される）
  → Copilot CLI は {"additionalContext": "..."} をトップレベルで出力する必要がある
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


def _copilot_context_output(event_name: str, additional_context: str) -> str:
    """Copilot CLI の additionalContext 形式 JSON を返す。

    Copilot CLI は SessionStart / UserPromptSubmit フックの出力から
    additionalContext をトップレベルで直接読む。Claude Code 形式の
    hookSpecificOutput ラッパーは これらのイベントでは無視される。

    Args:
        event_name: 未使用（Copilot CLI は hookEventName を参照しない）。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        {"additionalContext": "..."} 形式の JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return json.dumps(
        {"additionalContext": additional_context},
        ensure_ascii=False,
    )


# ハーネス → コンテキスト注入出力ビルダーのマッピング。
# Copilot CLI は additionalContext をトップレベルで読む（実機検証済み）。
# Codex は未検証のため Claude 形式を維持。
_CONTEXT_OUTPUT_BUILDERS = {
    "claude": _claude_context_output,
    "codex": _claude_context_output,
    "copilot": _copilot_context_output,
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


# PreToolUse ハーネス → コンテキスト注入出力ビルダーのマッピング。
# Copilot CLI は preToolUse の _vsCodeCompat ブランチで hookSpecificOutput を参照する
# （SessionStart/UserPromptSubmit とは異なり、トップレベル形式は無視される）。
# 実機検証後に copilot エントリのみ差し替え可能なデータ駆動構造にしてある。
_PRE_TOOL_USE_OUTPUT_BUILDERS = {
    "claude": _claude_context_output,
    "codex": _claude_context_output,
    "copilot": _claude_context_output,
    "unknown": _claude_context_output,
}


def adapt_pre_tool_use_context_output(additional_context: str) -> str:
    """PreToolUse コンテキスト注入出力を実行中ハーネスのプロトコルに合わせて生成する。

    全ハーネスで Claude 形式（hookSpecificOutput ラッパー）を返す第一仮説。
    Copilot CLI は preToolUse の _vsCodeCompat ブランチで hookSpecificOutput を
    参照するため（SessionStart/UserPromptSubmit とは異なる）、copilot も
    Claude 形式が正しい（実機検証後に copilot エントリのみ差し替え可）。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        ハーネスのプロトコルに適合した JSON 文字列。

    Raises:
        例外は発生しません。
    """
    builder = _PRE_TOOL_USE_OUTPUT_BUILDERS.get(detect_harness(), _claude_context_output)
    return builder("PreToolUse", additional_context)


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
