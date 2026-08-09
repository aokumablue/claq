"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合はハーネス別プロトコルでブロック
終了: 0 (許可) または 2 (ブロック。Copilot は emit_block_output により
      exit 0 + permissionDecision: deny の JSON へ変換される)

入力切り捨て時のブロックは本モジュール自身が判定する（launcher はインプロ
セスで直接ターゲットを実行するだけで stdin を代読しないため）。ファイル名
の保護判定と合わせ自己完結させている。
"""

from __future__ import annotations

from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    basename,
    emit_block_output,
    parse_json_object,
    read_raw_stdin_with_truncation,
)
from bluecore.lib.harness import extract_file_paths, normalize_tool_name

# matcher が "*"（全ツール）のため、書込み系ツールのみを対象にする早期 return に使う。
# normalize_tool_name() が Codex の apply_patch を "Edit" に、Copilot CLI の
# lowercase tool_name（write/edit/multiedit）を Claude Code 表記へ正規化するため、
# 正規化後に小文字化した値をこの集合と比較する。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

PROTECTED_FILES = {
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "biome.json",
    "biome.jsonc",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.yml",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlintrc",
}


def blocked_message_for_file(file_name: str) -> str:
    """保護されたファイルに対するブロックメッセージを生成する。

    Args:
        file_name: ブロックされたファイル名

    Returns:
        ブロックメッセージ文字列

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Modifying {file_name} is not allowed. "
        "Fix the source code to satisfy linter/formatter rules instead of "
        "weakening the config. If this is a legitimate config change, "
        "disable the config-protection hook temporarily."
    )


def _truncation_blocked_message(max_bytes: int) -> str:
    """入力切り捨て時のブロック理由メッセージを生成する。

    切り捨てられたペイロードで保護判定をすり抜けさせないための guard。

    Args:
        max_bytes: 入力の最大バイト数。

    Returns:
        ブロック理由メッセージ。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Hook input exceeded {max_bytes} bytes for pre:config-protection. "
        "Refusing to bypass protection on a truncated payload. "
        "Retry with a smaller edit."
    )


def main() -> int:
    """設定ファイルの編集を検知してブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、2: ブロック。Copilot では emit_block_output に
        より exit 0 + deny JSON へ変換される）

    Raises:
        例外は発生しません。
    """
    raw, truncated = read_raw_stdin_with_truncation()
    if truncated:
        # 切り捨てられたペイロードで保護判定をすり抜けさせない（fail-closed）。
        return emit_block_output(_truncation_blocked_message(MAX_STDIN_BYTES))

    data = parse_json_object(raw)
    if data:
        tool_name = str(data.get("tool_name") or "")
        if normalize_tool_name(tool_name).lower() not in _WRITE_TOOL_NAMES:
            return 0
        tool_input = data.get("tool_input")
        file_paths = extract_file_paths(tool_name, tool_input)
        if file_paths is None:
            # apply_patch のパッチがパース不能: 保護対象か判定できないため fail-closed
            return emit_block_output("BLOCKED: Could not determine target files from patch input.")
        if not file_paths and isinstance(tool_input, dict):
            # 旧形式の file フィールドのみ持つ入力を補完する
            legacy = str(tool_input.get("file") or "")
            if legacy:
                file_paths = [legacy]
        for file_path in file_paths:
            file_name = basename(file_path)
            if file_name in PROTECTED_FILES:
                return emit_block_output(blocked_message_for_file(file_name))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
