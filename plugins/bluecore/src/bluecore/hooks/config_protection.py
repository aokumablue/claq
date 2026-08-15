"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック。emit_block_output が stderr の理由と
      stdout の permissionDecision: deny JSON を同時に出す)

入力切り捨て時のブロックは本モジュール自身が判定する（launcher はインプロ
セスで直接ターゲットを実行するだけで stdin を代読しないため）。ファイル名
の保護判定と合わせ自己完結させている。
"""

from __future__ import annotations

from typing import Any

from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    basename,
    emit_block_output,
    parse_json_object,
    read_raw_stdin_with_truncation,
)
from bluecore.lib.harness import (
    extract_file_paths,
    extract_raw_tool_name,
    extract_tool_input,
    normalize_tool_name,
)

# matcher が "*"（全ツール）のため、書込み系ツールのみを対象にする早期 return に使う。
# normalize_tool_name() が Codex の apply_patch を "Edit" に、Copilot CLI の
# lowercase tool_name（write/edit/multiedit）を Claude Code 表記へ正規化するため、
# 正規化後に小文字化した値をこの集合と比較する。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

# ハーネスごとの入力コンテナキー。存在するキーを順に走査する。
_INPUT_CONTAINER_KEYS = ("tool_input", "toolArgs", "tool_args")

# apply_patch のパッチがパース不能なときの fail-closed 理由。
_UNPARSEABLE_PATCH_MESSAGE = "BLOCKED: Could not determine target files from patch input."

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


def _paths_from_container(tool_name: str, container: Any) -> list[str] | None:
    """1 つの入力コンテナから対象パスを取り出す。

    構造化パッチが判定不能なら None（呼び出し側は fail-closed）。
    file_path が無く dict なら旧 ``file`` キーを補完する。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        パス一覧。パッチ判定不能時は None。
    """
    file_paths = extract_file_paths(tool_name, container)
    if file_paths is None:
        return None
    if not file_paths and isinstance(container, dict):
        legacy = str(container.get("file") or "")
        if legacy:
            return [legacy]
    return file_paths


def _block_reason_for_container(tool_name: str, container: Any) -> str | None:
    """1 つの入力コンテナが保護対象ならブロック理由を返す。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        ブロック理由。保護対象でなければ None。パッチ判定不能時は
        fail-closed メッセージ。

    Raises:
        例外は発生しません。
    """
    file_paths = _paths_from_container(tool_name, container)
    if file_paths is None:
        return _UNPARSEABLE_PATCH_MESSAGE
    for file_path in file_paths:
        file_name = basename(file_path)
        if file_name in PROTECTED_FILES:
            return blocked_message_for_file(file_name)
    return None


def _block_reason(data: dict[str, Any]) -> str | None:
    """書込み系入力が保護対象ならブロック理由を返す。

    Args:
        data: フック stdin の dict。

    Returns:
        ブロック理由。対象外・保護対象なしなら None。

    Raises:
        例外は発生しません。
    """
    tool_name = extract_raw_tool_name(data)
    if normalize_tool_name(tool_name).lower() not in _WRITE_TOOL_NAMES:
        return None
    for key in _INPUT_CONTAINER_KEYS:
        if key not in data:
            continue
        reason = _block_reason_for_container(tool_name, extract_tool_input({key: data[key]}))
        if reason:
            return reason
    return None


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
    reason = _block_reason(data) if data else None
    if reason:
        return emit_block_output(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
