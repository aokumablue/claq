"""コマンド Markdown ファイルとその相互参照を検証する。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from devgear.ci.ci_common import REPO_ROOT, emit_error

DEFAULT_ROOT_DIR = REPO_ROOT
DEFAULT_COMMANDS_DIR = REPO_ROOT / "commands"
DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"
DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"


def _list_markdown_files(directory: Path) -> list[Path]:
    """ディレクトリ内の Markdown ファイルをリストアップする。

    Args:
        directory: スキャンするディレクトリパス

    Returns:
        Markdown ファイルのパスリスト

    Raises:
        例外は発生しません（存在しないディレクトリは空リストを返す）。
    """
    if not directory.exists():
        return []
    return [entry for entry in directory.iterdir() if entry.is_file() and entry.name.endswith(".md")]


def validate_commands(
    root_dir: str | Path = DEFAULT_ROOT_DIR,
    commands_dir: str | Path = DEFAULT_COMMANDS_DIR,
    agents_dir: str | Path = DEFAULT_AGENTS_DIR,
    skills_dir: str | Path = DEFAULT_SKILLS_DIR,
) -> int:
    """コマンド Markdown ファイルを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        root_dir: 処理に渡す root_dir の値です。
        commands_dir: 処理に渡す commands_dir の値です。
        agents_dir: 処理に渡す agents_dir の値です。
        skills_dir: 処理に渡す skills_dir の値です。

    Returns:
        処理結果を返します。

    Raises:
        例外は発生しません。
    """
    root = Path(root_dir)
    commands_path = Path(commands_dir) if Path(commands_dir).is_absolute() else root / commands_dir
    agents_path = Path(agents_dir) if Path(agents_dir).is_absolute() else root / agents_dir
    skills_path = Path(skills_dir) if Path(skills_dir).is_absolute() else root / skills_dir

    if not commands_path.exists():
        print("commands ディレクトリが見つかりません。検証をスキップします")
        return 0

    files = _list_markdown_files(commands_path)

    valid_commands = {file_path.stem for file_path in files}
    valid_agents = (
        {file_path.stem for file_path in _list_markdown_files(agents_path)} if agents_path.exists() else set()
    )
    valid_skills = {entry.name for entry in skills_path.iterdir() if entry.is_dir()} if skills_path.exists() else set()

    has_errors = False
    warn_count = 0

    for file_path in files:
        try:
            content = file_path.read_text(encoding="utf-8")
        except OSError as err:
            emit_error(f"{file_path.name} - ファイルの読み取りに失敗しました: {err}")
            has_errors = True
            continue

        if content.strip() == "":
            emit_error(f"{file_path.name} - コマンドファイルが空です")
            has_errors = True
            continue

        content_no_code_blocks = re.sub(r"```[\s\S]*?```", "", content)

        for line in content_no_code_blocks.splitlines():
            if re.search(r"creates:|would create:", line, re.I):
                continue
            for match in re.finditer(r"`/(c-[a-z0-9]+(?:-[a-z0-9]+)*)`", line):
                ref_name = match.group(1)
                if ref_name not in valid_commands:
                    emit_error(f"{file_path.name} - 存在しないコマンド /{ref_name} を参照しています")
                    has_errors = True

        for match in re.finditer(r"agents/(a-[a-z0-9]+(?:-[a-z0-9]+)*)\.md", content_no_code_blocks):
            ref_name = match.group(1)
            if ref_name not in valid_agents:
                emit_error(f"{file_path.name} - 存在しないエージェント agents/{ref_name}.md を参照しています")
                has_errors = True

        reserved_skill_roots = {"learned", "imported"}
        for match in re.finditer(r"skills/(s-[a-z0-9]+(?:-[a-z0-9]+)*)/", content_no_code_blocks):
            ref_name = match.group(1)
            if ref_name in reserved_skill_roots or ref_name in valid_skills:
                continue
            print(
                f"警告: {file_path.name} - skills/{ref_name}/ ディレクトリを参照しています（ローカルに見つかりません）"
            )
            warn_count += 1

        for match in re.finditer(
            r"^((?:(?:c|a)-[a-z0-9]+(?:-[a-z0-9]+)*)(?:\s*->\s*(?:c|a)-[a-z0-9]+(?:-[a-z0-9]+)*)+)$",
            content_no_code_blocks,
            re.M,
        ):
            agents = re.split(r"\s*->\s*", match.group(1))
            for agent in agents:
                if agent not in valid_agents:
                    emit_error(f'{file_path.name} - ワークフローが存在しないエージェント "{agent}" を参照しています')
                    has_errors = True

    if has_errors:
        return 1

    msg = f"{len(files)} 個のコマンドファイルを検証しました"
    if warn_count > 0:
        msg += f"（{warn_count} 件の警告）"
    print(msg)
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
    parser = argparse.ArgumentParser(description="Validate command markdown files")
    parser.add_argument("--root-dir", default=str(DEFAULT_ROOT_DIR))
    parser.add_argument("--commands-dir", default=str(DEFAULT_COMMANDS_DIR))
    parser.add_argument("--agents-dir", default=str(DEFAULT_AGENTS_DIR))
    parser.add_argument("--skills-dir", default=str(DEFAULT_SKILLS_DIR))
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
    return validate_commands(args.root_dir, args.commands_dir, args.agents_dir, args.skills_dir)


if __name__ == "__main__":
    raise SystemExit(main())
