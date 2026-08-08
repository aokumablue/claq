"""CI 検証用の共通ヘルパー。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]


def emit_error(message: str) -> None:
    """stderr にエラー行を書き出します。

    Args:
        message: 出力するエラーメッセージです。

    Returns:
        なし

    Raises:
        例外は発生しません。
    """
    print(f"エラー: {message}", file=sys.stderr)


def read_json(file_path: str | Path, label: str) -> Any:
    """JSON を読み取り、パースエラーを正規化します。

    Args:
        file_path: 読み取り対象の JSON ファイルパスです。
        label: エラーメッセージに使用するラベルです。

    Returns:
        パースされた JSON データを返します。

    Raises:
        ValueError: JSON のパースに失敗した場合に発生します。
    """
    try:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} の JSON 形式が不正です: {error}") from error


def is_non_empty_string(value: Any) -> bool:
    """値が空でない文字列かどうかを返します。

    Args:
        value: 判定対象の値です。

    Returns:
        空でない文字列の場合は True を返します。

    Raises:
        例外は発生しません。
    """
    return isinstance(value, str) and value.strip() != ""


def is_non_empty_string_array(value: Any) -> bool:
    """値が空でない文字列だけの空でない配列かどうかを返します。

    Args:
        value: 判定対象の値です。

    Returns:
        空でない文字列の配列の場合は True を返します。

    Raises:
        例外は発生しません。
    """
    return isinstance(value, list) and len(value) > 0 and all(is_non_empty_string(item) for item in value)
