"""フック出力を host 非依存の合併 JSON へ変換するアダプタ。

host 分岐は行わない。各 host が自分の読むキーだけを拾える形で、複数プロトコル
分の出力を同一 JSON に無条件で同梱する（合併出力）。Claude Code は
``hookSpecificOutput``、Copilot CLI はトップレベルの ``additionalContext`` を読む。
未知キーは無視される前提とし、実機未検証の host（Codex / Grok）にも同じ JSON を出す。

``modifiedResult`` は出力しない。claq の hook はコンテキスト注入と deny しか行わず、
実測していない host 契約を推測でキーとして足すと未検証の挙動を配布物へ持ち込むため。
"""

from __future__ import annotations

import json


def adapt_context_output(event_name: str, additional_context: str) -> str:
    """コンテキスト注入出力を host 非依存の合併 JSON として生成する。

    Args:
        event_name: hookEventName に設定するイベント名。
        additional_context: コンテキストに注入する追加文字列。

    Returns:
        additionalContext（トップレベル）と hookSpecificOutput を同時に
        含む合併 JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return json.dumps(
        {
            "additionalContext": additional_context,
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": additional_context,
            },
        },
        ensure_ascii=False,
    )


def adapt_deny_output(reason: str) -> str:
    """ツール実行ブロックの stdout 出力（Copilot CLI が読む permissionDecision JSON）を生成する。

    exit code 2（Claude Code / Codex 系が読む fail-closed シグナル）と stderr の理由は
    ``hook_common.emit_block_output`` が出す。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        permissionDecision=deny の JSON 文字列。

    Raises:
        例外は発生しません。
    """
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": reason},
        ensure_ascii=False,
    )
