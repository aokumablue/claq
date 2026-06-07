"""コマンド Markdown ファイルとその相互参照を検証する。"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from bluecore.ci.ci_common import REPO_ROOT, emit_error

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


def _check_command_references(file_name: str, content: str, valid_commands: set[str]) -> bool:
    """`/command` 形式のコマンド参照が実在するか検証する。

    Args:
        file_name: エラーメッセージに使うファイル名
        content: コードブロックを除去済みのコンテンツ
        valid_commands: 有効なコマンド名の集合

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    for line in content.splitlines():
        if re.search(r"creates:|would create:", line, re.I):
            continue
        for match in re.finditer(r"`/([a-z0-9]+(?:-[a-z0-9]+)*)`", line):
            ref_name = match.group(1)
            if ref_name not in valid_commands:
                emit_error(f"{file_name} - 存在しないコマンド /{ref_name} を参照しています")
                has_errors = True
    return has_errors


def _check_agent_references(file_name: str, content: str, valid_agents: set[str]) -> bool:
    """agents/<name>.md 形式のエージェント参照が実在するか検証する。

    Args:
        file_name: エラーメッセージに使うファイル名
        content: コードブロックを除去済みのコンテンツ
        valid_agents: 有効なエージェント名の集合

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    for match in re.finditer(r"agents/([a-z0-9]+(?:-[a-z0-9]+)*)\.md", content):
        ref_name = match.group(1)
        if ref_name not in valid_agents:
            emit_error(f"{file_name} - 存在しないエージェント agents/{ref_name}.md を参照しています")
            has_errors = True
    return has_errors


def _check_skill_references(file_name: str, content: str, valid_skills: set[str]) -> int:
    """skills/<name>/ 形式のスキル参照を確認し、未発見の件数を警告する。

    Args:
        file_name: 警告メッセージに使うファイル名
        content: コードブロックを除去済みのコンテンツ
        valid_skills: 有効なスキルディレクトリ名の集合

    Returns:
        発行した警告の件数

    Raises:
        例外は発生しません。
    """
    reserved_skill_roots = {"learned", "imported"}
    warn_count = 0
    for match in re.finditer(r"skills/([a-z0-9]+(?:-[a-z0-9]+)*)/", content):
        ref_name = match.group(1)
        if ref_name in reserved_skill_roots or ref_name in valid_skills:
            continue
        print(
            f"警告: {file_name} - skills/{ref_name}/ ディレクトリを参照しています（ローカルに見つかりません）"
        )
        warn_count += 1
    return warn_count


def _check_workflow_references(file_name: str, content: str, valid_agents: set[str]) -> bool:
    """`a -> b -> c` 形式のワークフロー参照のエージェントが実在するか検証する。

    Args:
        file_name: エラーメッセージに使うファイル名
        content: コードブロックを除去済みのコンテンツ
        valid_agents: 有効なエージェント名の集合

    Returns:
        エラーがあれば True、なければ False

    Raises:
        例外は発生しません。
    """
    has_errors = False
    for match in re.finditer(
        r"^((?:[a-z0-9]+(?:-[a-z0-9]+)*)(?:\s*->\s*[a-z0-9]+(?:-[a-z0-9]+)*)+)$",
        content,
        re.M,
    ):
        agents = re.split(r"\s*->\s*", match.group(1))
        for agent in agents:
            if agent not in valid_agents:
                emit_error(f'{file_name} - ワークフローが存在しないエージェント "{agent}" を参照しています')
                has_errors = True
    return has_errors


def _validate_command_file(
    file_path: Path, valid_commands: set[str], valid_agents: set[str], valid_skills: set[str]
) -> tuple[bool, int]:
    """単一のコマンドファイルの相互参照を検証する。

    Args:
        file_path: 検証するコマンドファイルのパス
        valid_commands: 有効なコマンド名の集合
        valid_agents: 有効なエージェント名の集合
        valid_skills: 有効なスキルディレクトリ名の集合

    Returns:
        (エラー有無, 発行した警告件数) のタプル

    Raises:
        例外は発生しません。
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except OSError as err:
        emit_error(f"{file_path.name} - ファイルの読み取りに失敗しました: {err}")
        return True, 0

    if content.strip() == "":
        emit_error(f"{file_path.name} - コマンドファイルが空です")
        return True, 0

    content_no_code_blocks = re.sub(r"```[\s\S]*?```", "", content)
    name = file_path.name
    has_errors = _check_command_references(name, content_no_code_blocks, valid_commands)
    if _check_agent_references(name, content_no_code_blocks, valid_agents):
        has_errors = True
    warn_count = _check_skill_references(name, content_no_code_blocks, valid_skills)
    if _check_workflow_references(name, content_no_code_blocks, valid_agents):
        has_errors = True
    return has_errors, warn_count


def _resolve_command_paths(
    root_dir: str | Path,
    commands_dir: str | Path,
    agents_dir: str | Path,
    skills_dir: str | Path,
) -> tuple[Path, Path, Path]:
    """各ディレクトリを絶対パスに解決して返す。

    Args:
        root_dir: リポジトリルート
        commands_dir: コマンドディレクトリ
        agents_dir: エージェントディレクトリ
        skills_dir: スキルディレクトリ

    Returns:
        (commands_path, agents_path, skills_path) のタプル
    """
    root = Path(root_dir)

    def _resolve(sub_dir: str | Path) -> Path:
        path = Path(sub_dir)
        return path if path.is_absolute() else root / sub_dir

    return _resolve(commands_dir), _resolve(agents_dir), _resolve(skills_dir)


def _build_valid_name_sets(
    commands_path: Path, agents_path: Path, skills_path: Path
) -> tuple[list[Path], set[str], set[str], set[str]]:
    """各ディレクトリから有効名の集合を構築して返す。

    Args:
        commands_path: コマンドディレクトリの絶対パス
        agents_path: エージェントディレクトリの絶対パス
        skills_path: スキルディレクトリの絶対パス

    Returns:
        (files, valid_commands, valid_agents, valid_skills) のタプル
    """
    files = _list_markdown_files(commands_path)
    valid_commands = {f.stem for f in files}
    valid_agents = (
        {f.stem for f in _list_markdown_files(agents_path)} if agents_path.exists() else set()
    )
    valid_skills = (
        {e.name for e in skills_path.iterdir() if e.is_dir()} if skills_path.exists() else set()
    )
    return files, valid_commands, valid_agents, valid_skills


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
    """
    commands_path, agents_path, skills_path = _resolve_command_paths(
        root_dir, commands_dir, agents_dir, skills_dir
    )
    if not commands_path.exists():
        print("commands ディレクトリが見つかりません。検証をスキップします")
        return 0
    files, valid_commands, valid_agents, valid_skills = _build_valid_name_sets(
        commands_path, agents_path, skills_path
    )
    has_errors = False
    warn_count = 0
    for file_path in files:
        file_errors, file_warns = _validate_command_file(file_path, valid_commands, valid_agents, valid_skills)
        if file_errors:
            has_errors = True
        warn_count += file_warns
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
