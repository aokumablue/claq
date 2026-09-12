"""フック出力を host 非依存の合併 JSON へ変換するアダプタ。

host 分岐は行わない。各 host が自分の読むキーだけを拾える形で、複数プロトコル
分の出力を同一 JSON に無条件で同梱する（合併出力）。Claude Code は
``hookSpecificOutput``、Copilot CLI は トップレベルの ``additionalContext``
を読む。未知キーは無視される前提とし、実機未検証の host（Codex / Grok）に
対しても同じ merged JSON を出力する。

``modifiedResult`` は出力しない。以前この docstring だけがそのキーに言及して
おり、実装（``additionalContext`` + ``hookSpecificOutput``）と食い違って
いた（release-verify 2026-09-03 の P2-016）。claq の hook はコンテキスト注入と
deny しか行わず、ツール結果の**書き換え**は 1 箇所も行わないため、実装が
正しく文書が誤っていた。実測していない host 契約を推測でキーとして足すのは、
未検証の挙動を配布物へ持ち込むことになるので行わない。
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



def emit_block(reason: str) -> tuple[int, str, str]:
    """ツール実行ブロックの出力を host 非依存の合併形式で組み立てる。

    exit code 2（Claude Code / Codex 系が読む fail-closed シグナル）と、
    stdout の permissionDecision JSON（Copilot CLI が読む契約）を同時に返す。

    Args:
        reason: ブロック理由（ユーザー / エージェントに提示される）。

    Returns:
        (exit_code, stdout, stderr) のタプル。呼び出し側はこの 3 つを
        そのまま出力・終了コードに使うこと。

    Raises:
        例外は発生しません。
    """
    payload = json.dumps(
        {"permissionDecision": "deny", "permissionDecisionReason": reason},
        ensure_ascii=False,
    )
    return 2, payload, reason
