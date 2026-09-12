"""エージェント Markdown ファイルの frontmatter を検証する。"""

from __future__ import annotations

import argparse
from pathlib import Path

from claq.ci.ci_common import REPO_ROOT, emit_error, extract_frontmatter

DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"


def _validate_agent_file(file_path: Path) -> bool:
    """単一のエージェント Markdown ファイルの frontmatter を検証する。

    `name` / `description` / `tools` の 3 項目を必須とする。

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

    for key in ("name", "description"):
        if not frontmatter.get(key):
            # description は ADR-0010 が dispatch の要と定めた項目で、欠けると
            # ホストがこのエージェントを選ぶ手がかりを失う。skills 側の
            # validate_skills は同じ 2 項目を必須にしており、agents だけ
            # 未検証だと「宣言が消えたのに緑」が agents 側でのみ起きる。
            emit_error(f"{file_path.name} - {key} が宣言されていません")
            return True

    if not frontmatter.get("tools"):
        # tools 未宣言だとエージェントはツール制限なしで起動し、Write/Bash 等
        # 実際には想定していない書き込み権限まで暗黙に継承する。宣言を
        # 必須にすることで、権限が本文の記述と食い違っていないかレビュー
        # できる場所（frontmatter）に強制的に載せる。
        emit_error(f"{file_path.name} - tools が宣言されていません（暗黙の全権継承を防ぐため必須）")
        return True

    return False


def validate_agents(agents_dir: str | Path = DEFAULT_AGENTS_DIR, *, optional: bool = False) -> int:
    """エージェント Markdown ファイルを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        agents_dir: 処理に渡す agents_dir の値です。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。
            既定の False では欠落を失敗として扱う（宣言がまるごと失われた破損を
            成功と報告しないため。F-03）。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    agents_path = Path(agents_dir)
    if not agents_path.exists():
        if optional:
            print("agents ディレクトリが見つかりません。--optional 指定のため検証をスキップします")
            return 0
        emit_error(f"agents ディレクトリが見つかりません: {agents_path}")
        return 1

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
    parser.add_argument(
        "--optional",
        action="store_true",
        help="対象パスが存在しない場合に失敗ではなくスキップする",
    )
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
    return validate_agents(args.agents_dir, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
