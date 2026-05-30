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


def normalize_relative_path(relative_path: Any) -> str:
    """JS バリデータと同じ方法でマニフェスト相対パスを正規化します。

    Args:
        relative_path: 正規化対象のパスです。

    Returns:
        正規化されたパス文字列を返します。

    Raises:
        例外は発生しません。
    """
    return str(relative_path).replace("\\", "/").rstrip("/")


def resolve_repo_path(repo_root: str | Path, relative_path: Any) -> Path:
    """マニフェスト相対パスをリポジトリルートに対して解決します。

    パストラバーサル（CWE-22）を防ぐため、解決後の絶対パスが
    ``repo_root`` 配下にあることを保証します。範囲外を指す場合は弾きます。

    Args:
        repo_root: リポジトリのルートディレクトリです。
        relative_path: 解決対象の相対パスです。

    Returns:
        解決された絶対パスを返します。

    Raises:
        ValueError: 解決後パスが repo_root の境界を越える場合に発生します。
    """
    normalized = normalize_relative_path(relative_path)
    root = Path(repo_root).resolve()
    candidate = (root / normalized.lstrip("/")).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise ValueError(f"path escapes repo root: {relative_path!r}") from error
    return candidate
