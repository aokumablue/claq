"""CI 検証用の共通ヘルパー。"""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

from claq.lib.core_utils import get_plugin_root, log
from claq.lib.harness import JSON_PARSE_FAILURES
from claq_dev.frontmatter import FrontmatterError, parse_yaml, split_frontmatter

PLUGIN_ROOT = get_plugin_root()


def emit_error(message: str) -> None:
    """stderr にエラー行を書き出す。"""
    log(f"エラー: {message}")


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


def list_markdown_files(directory: Path) -> list[Path]:
    """ディレクトリ直下の Markdown ファイルを返す。ディレクトリが無ければ空リスト。"""
    if not directory.exists():
        return []
    return [entry for entry in directory.iterdir() if entry.is_file() and entry.name.endswith(".md")]


def read_json(file_path: str | Path) -> Any:
    """JSON を読み取り、パースエラーをファイル名付きの ValueError に正規化する。"""
    path = Path(file_path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except JSON_PARSE_FAILURES as error:
        raise ValueError(f"{path.name} の JSON 形式が不正です: {error}") from error


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
        parsed = parse_yaml(split_frontmatter(content))
    except FrontmatterError:
        return None
    return parsed if isinstance(parsed, dict) else None


def frontmatter_spelling_errors(label: str, frontmatter: dict[str, object], known_fields: frozenset[str]) -> Iterator[str]:
    """frontmatter のキーのうち、既知フィールドのハイフン／アンダースコア取り違えを返す。

    自前パーサは綴り違いを「別のキー」として黙って受理するため、現実的な誤りであるこの形だけを名指しする。

    Args:
        label: メッセージの先頭に付ける対象名。
        frontmatter: 解析済みの frontmatter。
        known_fields: ハイフン区切りで書かれた既知フィールド名。

    Yields:
        綴り違い 1 件ごとのエラーメッセージ。
    """
    for key in frontmatter:
        swapped = key.replace("_", "-")
        if swapped != key and swapped in known_fields:
            yield f"{label} - frontmatter の {key} は {swapped} の綴り違い（黙って無視される）"
