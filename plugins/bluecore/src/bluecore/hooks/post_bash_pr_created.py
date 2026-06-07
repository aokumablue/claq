"""hosting service に応じた create コマンドで PR / MR が作成されたときに検出して報告します。

トリガー: post:bash
入力: bashコマンドと出力を含むJSON
    出力: PR / MR URLを含むstderr通知
終了: 0 (非ブロッキングな通知)
"""

from __future__ import annotations

from bluecore.hooks.hook_common import parse_json_object, read_raw_stdin, write_stderr, write_stdout
from bluecore.lib.git_hosting import (
    build_git_hosting_item_url,
    detect_git_hosting_service,
    extract_git_hosting_item_details,
    get_git_hosting_create_command,
    get_git_hosting_item_label,
    get_git_hosting_review_command,
    normalize_git_hosting_service,
)


def evaluate(raw_input: str, service: str | None = None) -> str:
    """create コマンドの結果を解析して通知を出す。

    Args:
        raw_input: フックに渡された生の入力文字列です。
        service: hosting service 名です。省略時は `git remote get-url origin` から自動判定します。

    Returns:
        元の入力をそのまま返します。

    Raises:
        例外は発生しません。
    """
    hosting_service = normalize_git_hosting_service(service or detect_git_hosting_service())
    data = parse_json_object(raw_input)

    if data:
        command = str((data.get("tool_input") or {}).get("command") or "")
        create_command = get_git_hosting_create_command(hosting_service)
        if create_command in command:
            output = str((data.get("tool_output") or {}).get("output") or "")
            details = extract_git_hosting_item_details(hosting_service, output)
            if details:
                repo, item_number = details
                item_label = get_git_hosting_item_label(hosting_service)
                review_command = get_git_hosting_review_command(hosting_service)
                item_url = build_git_hosting_item_url(hosting_service, repo, item_number)
                write_stderr(f"[Hook] {item_label} created: {item_url}\n")
                write_stderr(f"[Hook] To review: {review_command} {item_number} --repo {repo}\n")

    return raw_input


def main() -> int:
    """PR 作成後に URL とレビューコマンドを表示する。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 成功）

    Raises:
        例外は発生しません。
    """
    raw = read_raw_stdin()
    output = evaluate(raw)
    write_stdout(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
