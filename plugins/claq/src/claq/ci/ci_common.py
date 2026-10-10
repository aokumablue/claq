"""CI 検証用の共通ヘルパー。"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from claq.lib.frontmatter import FrontmatterError, parse_yaml, split_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[3]


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


def report_missing(label: str, path: Path, optional: bool) -> int:
    """検証対象が無いときの終了コードを返す。

    ``optional`` ならスキップを表示して 0 を返す。既定では欠落を失敗として扱う
    （宣言がまるごと失われた破損を成功と報告しないため）。

    Args:
        label: メッセージ先頭の対象名（``"skills ディレクトリ"`` など）。
        path: 見つからなかったパス。
        optional: ``--optional`` が指定されたか。
    """
    if optional:
        print(f"{label}が見つかりません。--optional 指定のため検証をスキップします")
        return 0
    emit_error(f"{label}が見つかりません: {path}")
    return 1


def parse_validator_args(description: str, argv: Sequence[str] | None, **path_defaults: Path) -> argparse.Namespace:
    """validator 共通の CLI（``--optional`` とパス指定フラグ）を解析する。

    Args:
        description: ``--help`` に出す説明。
        argv: 引数リスト。None なら ``sys.argv``。
        path_defaults: ``skills_dir=...`` の形のフラグ名（``--skills-dir``）と既定値。
    """
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument(
        "--optional",
        action="store_true",
        help="対象パスが存在しない場合に失敗ではなくスキップする",
    )
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
    """先頭の --- で囲まれた frontmatter を辞書として返す。無ければ None。

    解釈できない行を読み飛ばして部分結果を返す寛容モードで解析する。ただし
    **重複キーは寛容モードでも誤り**として扱い、None（＝不合格）を返す。先勝ちで
    黙認すると、検証器が `tools: Read` を見る一方で後勝ちのホストは `tools: Bash`
    を見る、というパーサ差分になる。
    """
    try:
        block = split_frontmatter(content)
    except FrontmatterError:
        return None
    # 重複キーは `parse_yaml` が寛容モードでも None（またはキーを落とした dict）
    # にして返すので、ここで捕捉すべき例外は無い。
    data = parse_yaml(block, lenient=True)
    return data if isinstance(data, dict) else None
