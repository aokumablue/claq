"""エージェント Markdown ファイルの frontmatter を検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT, emit_error
from bluecore.lib.frontmatter import FrontmatterError, parse_yaml, split_frontmatter

DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"


def extract_frontmatter(content: str) -> dict[str, object] | None:
    """先頭の --- で囲まれた frontmatter を辞書として返す。無ければ None。

    解釈できない行を読み飛ばして部分結果を返す寛容モードで解析する。

    Args:
        content: Markdown ファイルの全文。

    Returns:
        frontmatter の辞書。frontmatter が無い、または辞書でない場合は None。
    """
    try:
        block = split_frontmatter(content)
    except FrontmatterError:
        return None
    data = parse_yaml(block, lenient=True)
    return data if isinstance(data, dict) else None


def _validate_agent_file(file_path: Path) -> bool:
    """単一のエージェント Markdown ファイルに frontmatter があるか検証する。

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

    if not frontmatter.get("tools"):
        # tools 未宣言だとエージェントはツール制限なしで起動し、Write/Bash 等
        # 実際には想定していない書き込み権限まで暗黙に継承する。宣言を
        # 必須にすることで、権限が本文の記述と食い違っていないかレビュー
        # できる場所（frontmatter）に強制的に載せる。
        emit_error(f"{file_path.name} - tools が宣言されていません（暗黙の全権継承を防ぐため必須）")
        return True

    return False


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
