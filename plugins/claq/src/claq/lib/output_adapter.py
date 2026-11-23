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


def adapt_deny_output(reason: str) -> str:
    """ツール実行ブロックの stdout 出力（Copilot CLI が読む permissionDecision JSON）を生成する。

    exit code 2（Claude Code / Codex 系が読む fail-closed シグナル）と stderr の理由は
    ``hook_common.emit_block_output`` が出す。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        permissionDecision=deny の JSON 文字列。
    """
    return json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": reason},
        ensure_ascii=False,
    )


def print_session_start_output(additional_context: str) -> None:
    """SessionStart のコンテキスト注入出力を host 非依存の合併 JSON として stdout に書き出す。

    additionalContext（トップレベル）と hookSpecificOutput を同時に含める。

    Args:
        additional_context: コンテキストに注入する追加文字列。

    Raises:
        OSError: パイプが閉じている場合（BrokenPipeError を含む）。
        UnicodeEncodeError: 出力先のエンコーディングで表現できない場合。
    """
    payload = {
        "additionalContext": additional_context,
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": additional_context},
    }
    print(json.dumps(payload, ensure_ascii=False))
