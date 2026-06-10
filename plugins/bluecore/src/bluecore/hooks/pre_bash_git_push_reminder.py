"""git push 前に変更をレビューするよう通知

トリガー: pre:bash
入力: bash コマンドを含む JSON
出力: git push が検出された場合は stderr に通知
終了: 0（ノンブロッキング通知）
"""

from __future__ import annotations

import re

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr


def main() -> int:
    """git push 前にレビューを促すリマインダーを表示する。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 成功）

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    data = parse_json_object(raw)

    if data:
        command = str((data.get("tool_input") or {}).get("command") or "")
        if re.search(r"\bgit\s+push\b", command):
            write_stderr("[Hook] Review changes before push...\n")
            write_stderr("[Hook] Continuing with push (remove this hook to add interactive review)\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
