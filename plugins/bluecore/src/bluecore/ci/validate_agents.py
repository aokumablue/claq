"""エージェント Markdown ファイルに必須 frontmatter があるか検証する。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT, emit_error, is_non_empty_string

DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"
REQUIRED_FIELDS = ["model", "tools"]
VALID_MODELS = ["haiku", "sonnet", "opus"]


def extract_frontmatter(content: str) -> dict[str, str] | None:
    """シンプルな YAML frontmatter ブロックを抽出する。

    Args:
        content: Markdown ファイルの全文。

    Returns:
        先頭の --- で囲まれた frontmatter を key: value 辞書として返す。
        frontmatter が無ければ None。
    """
    clean_content = content.lstrip("\ufeff")
    match = re.match(r"^---\r?\n([\s\S]*?)\r?\n---", clean_content)
    if not match:
        return None

    frontmatter: dict[str, str] = {}
    for line in match.group(1).splitlines():
        colon_idx = line.find(":")
        if colon_idx > 0:
            key = line[:colon_idx].strip()
            value = line[colon_idx + 1 :].strip().strip("\"'").strip()
            frontmatter[key] = value
    return frontmatter


def _validate_agent_file(file_path: Path) -> bool:
    """単一のエージェント Markdown ファイルを検証する。

    Args:
        file_path: 検証するエージェントファイルのパス

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as err:
        emit_error(f"{file_path.name} - ファイルの読み取りに失敗しました: {err}")
        return True

    frontmatter = extract_frontmatter(content)
    if frontmatter is None:
        emit_error(f"{file_path.name} - フロントマターがありません")
        return True

    has_errors = False
    for field in REQUIRED_FIELDS:
        if not is_non_empty_string(frontmatter.get(field)):
            emit_error(f"{file_path.name} - 必須フィールドが不足しています: {field}")
            has_errors = True

    model = frontmatter.get("model")
    if model and model not in VALID_MODELS:
        emit_error(
            f"{file_path.name} - モデル '{model}' は無効です。次のいずれかである必要があります: {', '.join(VALID_MODELS)}"
        )
        has_errors = True

    return has_errors


def validate_agents(agents_dir: str | Path = DEFAULT_AGENTS_DIR) -> int:
    """エージェント Markdown ファイルを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        agents_dir: 処理に渡す agents_dir の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    agents_path = Path(agents_dir)
    if not agents_path.exists():
        print("agents ディレクトリが見つかりません。検証をスキップします")
        return 0

    files = [entry for entry in agents_path.iterdir() if entry.is_file() and entry.name.endswith(".md")]
    has_errors = False

    for file_path in files:
        if _validate_agent_file(file_path):
            has_errors = True

    if has_errors:
        return 1

    print(f"{len(files)} 個のエージェントファイルを検証しました")
    return 0


def build_parser() -> argparse.ArgumentParser:
    """CLI パーサーを構築する。

    Args:
        引数はありません。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    parser = argparse.ArgumentParser(description="Validate agent markdown files")
    parser.add_argument("--agents-dir", default=str(DEFAULT_AGENTS_DIR))
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。

    Args:
        argv: 処理に渡す argv の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    args = build_parser().parse_args(argv)
    return validate_agents(args.agents_dir)


if __name__ == "__main__":
    raise SystemExit(main())
