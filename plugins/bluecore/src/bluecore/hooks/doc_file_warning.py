"""リポジトリルートに作成されたアドホックなドキュメントファイルを警告します。

トリガー: pre:write
入力: 書き込まれるファイルパスを含むJSON
出力: ルートにアドホックなドキュメントファイルが検出されたらstderrに警告
終了: 0 (警告のみ、非ブロッキング)
"""

from __future__ import annotations

import re
from pathlib import Path

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr

ADHOC_FILENAMES = re.compile(r"^(NOTES|TODO|SCRATCH|TEMP|DRAFT|BRAINSTORM|SPIKE|DEBUG|WIP)\.(md|txt)$")
STRUCTURED_DIRS = re.compile(
    r"(^|/)(docs|\.claude|\.github|\.gitlab|commands|skills|benchmarks|templates|\.history|memory)/"
)


def is_suspicious_doc_path(file_path: str) -> bool:
    """ファイルパスがアドホックなドキュメントファイル名か判定する。

    Args:
        file_path: チェックするファイルパス

    Returns:
        アドホックなドキュメントファイル名の場合は True

    Raises:
        例外は発生しません。
    """
    normalized = file_path.replace("\\", "/")
    basename = Path(normalized).name

    if not re.search(r"\.(md|txt)$", basename):
        return False
    if not ADHOC_FILENAMES.search(basename):
        return False
    if STRUCTURED_DIRS.search(normalized):
        return False
    return True


def main() -> int:
    """アドホックなドキュメントファイル名の作成を検出して警告を出力する。

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
        file_path = str((data.get("tool_input") or {}).get("file_path") or "")
        if file_path and is_suspicious_doc_path(file_path):
            write_stderr("[Hook] WARNING: Ad-hoc documentation filename detected\n")
            write_stderr(f"[Hook] File: {file_path}\n")
            write_stderr(
                "[Hook] Consider using a structured path (e.g. docs/, .claude/, skills/, .github/, .gitlab/, benchmarks/, templates)"
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
