"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合はstderrに警告
終了: 0 (許可) または 2 (ブロック)

入力の切り捨て判定は run_with_flags.py 側で実施する（
`_TRUNCATION_GUARD_HOOK_IDS` 参照）。本モジュールはファイル名のみで判定する。
"""

from __future__ import annotations

from bluecore.hooks.hook_common import basename, parse_json_object, read_raw_stdin, write_stderr
from bluecore.lib.harness import extract_file_paths

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
    # ONNX ダウンロード設定。model_url / sha256 / ssl_no_verify が
    # ダウンロード完全性検証の信頼アンカーのため、エージェントによる
    # 書き換え（URL 差し替え + 偽ハッシュ + 検証無効化）をブロックする
    "onnx.json",
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


def main() -> int:
    """設定ファイルの編集を検知してブロックする。

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
        tool_name = str(data.get("tool_name") or "")
        tool_input = data.get("tool_input") or {}
        file_paths = extract_file_paths(tool_name, tool_input)
        if file_paths is None:
            # apply_patch のパッチがパース不能: 保護対象か判定できないため fail-closed
            write_stderr("BLOCKED: Could not determine target files from patch input.\n")
            return 2
        if not file_paths:
            # 旧形式の file フィールドのみ持つ入力を補完する
            legacy = str(tool_input.get("file") or "")
            if legacy:
                file_paths = [legacy]
        for file_path in file_paths:
            file_name = basename(file_path)
            if file_name in PROTECTED_FILES:
                write_stderr(blocked_message_for_file(file_name) + "\n")
                return 2

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
