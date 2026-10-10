"""コマンド Markdown ファイルとその相互参照を検証する。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from claq.ci.ci_common import (
    REPO_ROOT,
    emit_error,
    parse_validator_args,
    read_or_report,
    report_errors,
    report_missing,
)

DEFAULT_ROOT_DIR = REPO_ROOT
DEFAULT_COMMANDS_DIR = REPO_ROOT / "commands"
DEFAULT_AGENTS_DIR = REPO_ROOT / "agents"
DEFAULT_SKILLS_DIR = REPO_ROOT / "skills"

_RESERVED_SKILL_ROOTS = {"learned", "imported"}


def _list_markdown_files(directory: Path) -> list[Path]:
    """ディレクトリ直下の Markdown ファイルを返す。ディレクトリが無ければ空リスト。"""
    if not directory.exists():
        return []
    return [entry for entry in directory.iterdir() if entry.is_file() and entry.name.endswith(".md")]


_COMMAND_REFERENCE_PATTERN = re.compile(r"`/([a-z0-9]+(?:-[a-z0-9]+)*)(?:\s[^`]*)?`")
"""バッククォートで囲まれたコマンド参照。名前の後に引数が続く形も捕捉する。

閉じバッククォートが名前の直後に来る形だけを見ると、引数付きの参照
（``/does-not-exist arg``）が検証をすり抜ける。引数部は ``\\s`` 始まりに
限るので、``/usr/bin/env`` のようなパスは一致しない。

適用先は ``commands/*.md`` だけで、``agents/`` ``skills/`` の md にある
``/command`` 参照はどのバリデータも検証しない。
"""


def _command_reference_errors(file_name: str, content: str, valid_commands: set[str]) -> Iterator[str]:
    """存在しない ``/command`` 参照の誤りを返す（``creates:`` 行は対象外）。"""
    for line in content.splitlines():
        if re.search(r"creates:|would create:", line, re.I):
            continue
        for match in _COMMAND_REFERENCE_PATTERN.finditer(line):
            if match.group(1) not in valid_commands:
                yield f"{file_name} - 存在しないコマンド /{match.group(1)} を参照しています"


def _agent_reference_errors(file_name: str, content: str, valid_agents: set[str]) -> Iterator[str]:
    """存在しない ``agents/<name>.md`` 参照の誤りを返す。"""
    for ref_name in re.findall(r"agents/([a-z0-9]+(?:-[a-z0-9]+)*)\.md", content):
        if ref_name not in valid_agents:
            yield f"{file_name} - 存在しないエージェント agents/{ref_name}.md を参照しています"


def _warn_skill_references(file_name: str, content: str, valid_skills: set[str]) -> int:
    """ローカルに無い ``skills/<name>/`` 参照を stdout へ警告し、件数を返す。"""
    warn_count = 0
    for ref_name in re.findall(r"skills/([a-z0-9]+(?:-[a-z0-9]+)*)/", content):
        if ref_name in _RESERVED_SKILL_ROOTS or ref_name in valid_skills:
            continue
        print(f"警告: {file_name} - skills/{ref_name}/ ディレクトリを参照しています（ローカルに見つかりません）")
        warn_count += 1
    return warn_count


def _workflow_reference_errors(file_name: str, content: str, valid_agents: set[str]) -> Iterator[str]:
    """``a -> b -> c`` 形式のワークフローに現れる、存在しないエージェントの誤りを返す。"""
    for match in re.finditer(
        r"^((?:[a-z0-9]+(?:-[a-z0-9]+)*)(?:\s*->\s*[a-z0-9]+(?:-[a-z0-9]+)*)+)$",
        content,
        re.M,
    ):
        for agent in re.split(r"\s*->\s*", match.group(1)):
            if agent not in valid_agents:
                yield f'{file_name} - ワークフローが存在しないエージェント "{agent}" を参照しています'


def _validate_command_file(
    file_path: Path, valid_commands: set[str], valid_agents: set[str], valid_skills: set[str]
) -> tuple[bool, int]:
    """単一のコマンドファイルの相互参照を検証し、(エラー有無, 警告件数) を返す。"""
    name = file_path.name
    content = read_or_report(file_path, name)
    if content is None:
        return True, 0
    if not content.strip():
        emit_error(f"{name} - コマンドファイルが空です")
        return True, 0

    body = re.sub(r"```[\s\S]*?```", "", content)
    has_errors = report_errors(_command_reference_errors(name, body, valid_commands))
    has_errors |= report_errors(_agent_reference_errors(name, body, valid_agents))
    warn_count = _warn_skill_references(name, body, valid_skills)
    has_errors |= report_errors(_workflow_reference_errors(name, body, valid_agents))
    return has_errors, warn_count


def validate_commands(
    root_dir: str | Path = DEFAULT_ROOT_DIR,
    commands_dir: str | Path = DEFAULT_COMMANDS_DIR,
    agents_dir: str | Path = DEFAULT_AGENTS_DIR,
    skills_dir: str | Path = DEFAULT_SKILLS_DIR,
    *,
    optional: bool = False,
) -> int:
    """コマンド Markdown ファイルを検証し、JS バリデータと同じメッセージを表示する。

    Args:
        root_dir: 相対指定の各ディレクトリを解決する基準。
        commands_dir: コマンドディレクトリ。
        agents_dir: エージェントディレクトリ。
        skills_dir: スキルディレクトリ。
        optional: True なら commands ディレクトリが無い場合に検証をスキップして 0 を返す。
            hooks / skills / agents の 3 バリデータと同じく既定では欠落を失敗にする。
            commands だけ無条件スキップにすると ``commands/`` を消すだけで相互参照
            検証チェーン全体が「成功」を報告するため。

    Returns:
        終了コード（0 = 合格、1 = 不合格）。
    """
    # 絶対パスとの結合は右辺がそのまま残るので、相対指定だけが root_dir 基準になる。
    root = Path(root_dir)
    commands_path, agents_path, skills_path = root / commands_dir, root / agents_dir, root / skills_dir
    if not commands_path.exists():
        return report_missing("commands ディレクトリ", commands_path, optional)

    files = _list_markdown_files(commands_path)
    valid_commands = {path.stem for path in files}
    valid_agents = {path.stem for path in _list_markdown_files(agents_path)}
    valid_skills = {entry.name for entry in skills_path.iterdir() if entry.is_dir()} if skills_path.exists() else set()

    has_errors = False
    warn_count = 0
    for file_path in files:
        file_errors, file_warns = _validate_command_file(file_path, valid_commands, valid_agents, valid_skills)
        has_errors |= file_errors
        warn_count += file_warns
    if has_errors:
        return 1
    suffix = f"（{warn_count} 件の警告）" if warn_count else ""
    print(f"{len(files)} 個のコマンドファイルを検証しました{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    args = parse_validator_args(
        "Validate command markdown files",
        argv,
        root_dir=DEFAULT_ROOT_DIR,
        commands_dir=DEFAULT_COMMANDS_DIR,
        agents_dir=DEFAULT_AGENTS_DIR,
        skills_dir=DEFAULT_SKILLS_DIR,
    )
    return validate_commands(args.root_dir, args.commands_dir, args.agents_dir, args.skills_dir, optional=args.optional)


if __name__ == "__main__":
    raise SystemExit(main())
