"""--no-verifyまたは-nフラグを持つgitコマンドをブロックします。

トリガー: pre:bash
入力: bashコマンドを含むJSON
出力: ブロック時はstderrにエラーメッセージ
終了: no-verify検出時は2 (実行をブロック)、それ以外は0
"""

from __future__ import annotations

import re

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr

NO_VERIFY_RE = re.compile(r"\bgit\s+(commit|push)\b.*?(--no-verify|-n)\b", re.IGNORECASE)
QUOTED_RE = re.compile(r""""[^"]*"|'[^']*'""")


def strip_quoted(command: str) -> str:
    """シェルコマンドからクォート文字列リテラルを除去する。

    コミットメッセージ等のクォート内に現れる ``-n`` / ``--no-verify`` への
    誤検知を防ぐ。

    Args:
        command: 対象のシェルコマンド文字列。

    Returns:
        クォート文字列を除去したコマンド文字列。

    Raises:
        例外は発生しません。
    """
    return QUOTED_RE.sub("", command)


def main() -> int:
    """git コマンドで --no-verify フラグの使用をブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、2: ブロック）

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    data = parse_json_object(raw)

    if data:
        command = str((data.get("tool_input") or {}).get("command") or "")
        if NO_VERIFY_RE.search(strip_quoted(command)):
            write_stderr("[Hook] BLOCKED: git hook bypass flags are not allowed\n")
            return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
