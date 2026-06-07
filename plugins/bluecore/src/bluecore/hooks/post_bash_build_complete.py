"""ビルドコマンドが正常に完了したときに通知します。

トリガー: post:bash
入力: bashコマンドと出力を含むJSON
出力: ビルドが検出されたらstderrに通知
終了: 0 (非ブロッキングな通知)
"""

from __future__ import annotations

import re

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout


def main() -> int:
    """ビルド完了後にバックグラウンド分析を通知する。

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
        if re.search(r"(npm run build|pnpm build|yarn build)", command):
            write_stderr("[Hook] Build completed - async analysis running in background\n")

    write_stdout(raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
