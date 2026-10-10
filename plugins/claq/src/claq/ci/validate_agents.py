"""エージェント Markdown ファイルの frontmatter を検証する。"""

from __future__ import annotations

from pathlib import Path

from claq.ci.ci_common import (
    REPO_ROOT,
    emit_error,
    extract_frontmatter,
    parse_validator_args,
    read_or_report,
    report_missing,
)

DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"


def _validate_agent_file(file_path: Path) -> bool:
    """単一のエージェント Markdown の frontmatter（name / description / tools 必須）を検証し、誤りがあれば True。"""
    content = read_or_report(file_path, file_path.name)
    if content is None:
        return True
    frontmatter = extract_frontmatter(content)
    if frontmatter is None:
        emit_error(f"{file_path.name} - フロントマターがありません")
        return True
    for key in ("name", "description"):
        if not frontmatter.get(key):
            # description は docs/adr/definition-and-subagent-design.md が dispatch の要と定めた項目で、欠けると
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
        agents_dir: 検証するエージェントディレクトリ。
        optional: True なら対象パスが存在しない場合に検証をスキップして 0 を返す。

    Returns:
        終了コード（0 = 合格、1 = 不合格）。
    """
    agents_path = Path(agents_dir)
    if not agents_path.exists():
        return report_missing("agents ディレクトリ", agents_path, optional)
    files = [entry for entry in agents_path.iterdir() if entry.is_file() and entry.name.endswith(".md")]
    # 全ファイルの誤りを出し切るため、短絡させずに検証する。
    if [file_path for file_path in files if _validate_agent_file(file_path)]:
        return 1
    print(f"{len(files)} 個のエージェントファイルを検証しました")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    args = parse_validator_args("Validate agent markdown files", argv, agents_dir=DEFAULT_AGENTS_DIR)
    return validate_agents(args.agents_dir, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
