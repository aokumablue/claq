"""CI 検証用の共通ヘルパー。"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ple4.lib.frontmatter import FrontmatterError, parse_yaml, split_frontmatter

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


def extract_frontmatter(content: str) -> dict[str, object] | None:
    """先頭の --- で囲まれた frontmatter を辞書として返す。無ければ None。

    解釈できない行を読み飛ばして部分結果を返す寛容モードで解析する。ただし
    **重複キーは寛容モードでも誤り**として扱い、None（＝不合格）を返す。先勝ちで
    黙認すると、検証器が `tools: Read` を見る一方で後勝ちのホストは `tools: Bash`
    を見る、というパーサ差分になる。

    Args:
        content: Markdown ファイルの全文。

    Returns:
        frontmatter の辞書。frontmatter が無い、または辞書でない場合は None。
    """
    try:
        block = split_frontmatter(content)
    except FrontmatterError:
        return None
    # 重複キーは `parse_yaml` が寛容モードでも None（またはキーを落とした dict）
    # にして返すので、ここで捕捉すべき例外は無い。
    data = parse_yaml(block, lenient=True)
    return data if isinstance(data, dict) else None
