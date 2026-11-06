"""CI 検証用の共通ヘルパー。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from claq.lib.core_utils import get_plugin_root
from claq.lib.frontmatter import FrontmatterError, parse_yaml, split_frontmatter

PLUGIN_ROOT = get_plugin_root()


def emit_error(message: str) -> None:
    """stderr にエラー行を書き出す。"""
    print(f"エラー: {message}", file=sys.stderr)


def report_errors(messages: Iterable[str]) -> bool:
    """各メッセージを順に stderr へ書き出し、1 件でもあれば True を返す。"""
    has_errors = False
    for message in messages:
        emit_error(message)
        has_errors = True
    return has_errors


def report_missing(label: str, path: Path) -> int:
    """検証対象が無いときの終了コードを返す（宣言がまるごと失われた破損を成功と報告しない）。

    Args:
        label: メッセージ先頭の対象名（``"skills ディレクトリ"`` など）。
        path: 見つからなかったパス。
    """
    emit_error(f"{label}が見つかりません: {path}")
    return 1


def read_or_report(path: Path, label: str) -> str | None:
    """UTF-8 で読む。読めなければ ``label`` 付きのエラーを書き出して None を返す。"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        emit_error(f"{label} - ファイルの読み取りに失敗しました: {err}")
        return None


def parse_validator_args(description: str, argv: Sequence[str] | None, **path_defaults: Path) -> argparse.Namespace:
    """validator 共通の CLI（パス指定フラグ）を解析する。

    Args:
        description: ``--help`` に出す説明。
        argv: 引数リスト。None なら ``sys.argv``。
        path_defaults: ``skills_dir=...`` の形のフラグ名（``--skills-dir``）と既定値。
    """
    parser = argparse.ArgumentParser(description=description)
    for dest, default in path_defaults.items():
        parser.add_argument(f"--{dest.replace('_', '-')}", default=str(default))
    return parser.parse_args(argv)


def read_json(file_path: str | Path, label: str) -> Any:
    """JSON を読み取り、パースエラーを ``label`` 付きの ValueError に正規化する。"""
    try:
        return json.loads(Path(file_path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"{label} の JSON 形式が不正です: {error}") from error


def is_non_empty_string(value: Any) -> bool:
    """値が空白以外を含む文字列かどうかを返す。"""
    return isinstance(value, str) and value.strip() != ""


def is_non_empty_string_array(value: Any) -> bool:
    """値が空でない文字列だけの空でない配列かどうかを返す。"""
    return isinstance(value, list) and len(value) > 0 and all(is_non_empty_string(item) for item in value)


def extract_frontmatter(content: str) -> dict[str, object] | None:
    """先頭の --- で囲まれた frontmatter を辞書として返す。

    Returns:
        frontmatter の辞書。frontmatter が無い・構文を解釈できない（重複キーを含む）・
        mapping でない場合は None（validators はこれを不合格として報告する）。
    """
    try:
        data = parse_yaml(split_frontmatter(content))
    except FrontmatterError:
        return None
    return data if isinstance(data, dict) else None
