"""コマンド Markdown ファイルとその相互参照を検証する。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from claq.ci.ci_common import (
    PLUGIN_ROOT,
    emit_error,
    extract_frontmatter,
    list_markdown_files,
    parse_validator_args,
    read_or_report,
    report_errors,
    report_missing,
)

DEFAULT_ROOT_DIR = PLUGIN_ROOT
DEFAULT_COMMANDS_DIR = PLUGIN_ROOT / "commands"
DEFAULT_AGENTS_DIR = PLUGIN_ROOT / "agents"
DEFAULT_SKILLS_DIR = PLUGIN_ROOT / "skills"

_RESERVED_SKILL_ROOTS = {"learned", "imported"}

# commands の frontmatter で意味を持つキー。Claude Code は未知のキーを黙って無視するので、
# `argument_hint` のようなアンダースコア綴りは宣言したつもりで効かない（補完時のヒントが出ない）。
_KNOWN_FRONTMATTER_FIELDS = frozenset({"name", "description", "command", "argument-hint"})


_KEBAB_NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"
"""コマンド・エージェント・スキルの名前（小文字英数字のハイフン区切り）に一致する正規表現の断片。"""

_COMMAND_REFERENCE_PATTERN = re.compile(rf"`/({_KEBAB_NAME})(?:\s[^`]*)?`")
"""バッククォートで囲まれたコマンド参照。名前の後に引数が続く形も捕捉する。

閉じバッククォートが名前の直後に来る形だけを見ると、引数付きの参照
（``/does-not-exist arg``）が検証をすり抜ける。引数部は ``\\s`` 始まりに
限るので、``/usr/bin/env`` のようなパスは一致しない。

適用先は ``commands/*.md`` だけで、``agents/`` ``skills/`` の md にある
``/command`` 参照はどのバリデータも検証しない。
"""

_CREATES_LINE_PATTERN = re.compile(r"creates:|would create:", re.IGNORECASE)
_AGENT_REFERENCE_PATTERN = re.compile(rf"agents/({_KEBAB_NAME})\.md")
_SKILL_REFERENCE_PATTERN = re.compile(rf"skills/({_KEBAB_NAME})/")
_WORKFLOW_PATTERN = re.compile(rf"^((?:{_KEBAB_NAME})(?:\s*->\s*{_KEBAB_NAME})+)$", re.MULTILINE)
_WORKFLOW_ARROW_PATTERN = re.compile(r"\s*->\s*")
_CODE_FENCE_PATTERN = re.compile(r"```[\s\S]*?```")


def _command_reference_errors(file_name: str, content: str, valid_commands: set[str]) -> Iterator[str]:
    """存在しない ``/command`` 参照の誤りを返す（``creates:`` 行は対象外）。"""
    for line in content.splitlines():
        if _CREATES_LINE_PATTERN.search(line):
            continue
        for match in _COMMAND_REFERENCE_PATTERN.finditer(line):
            if match.group(1) not in valid_commands:
                yield f"{file_name} - 存在しないコマンド /{match.group(1)} を参照しています"


def _agent_reference_errors(file_name: str, content: str, valid_agents: set[str]) -> Iterator[str]:
    """存在しない ``agents/<name>.md`` 参照の誤りを返す。"""
    for ref_name in _AGENT_REFERENCE_PATTERN.findall(content):
        if ref_name not in valid_agents:
            yield f"{file_name} - 存在しないエージェント agents/{ref_name}.md を参照しています"


def _warn_skill_references(file_name: str, content: str, valid_skills: set[str]) -> int:
    """ローカルに無い ``skills/<name>/`` 参照を stdout へ警告し、件数を返す。"""
    warn_count = 0
    for ref_name in _SKILL_REFERENCE_PATTERN.findall(content):
        if ref_name in _RESERVED_SKILL_ROOTS or ref_name in valid_skills:
            continue
        print(f"警告: {file_name} - skills/{ref_name}/ ディレクトリを参照しています（ローカルに見つかりません）")
        warn_count += 1
    return warn_count


def _workflow_reference_errors(file_name: str, content: str, valid_agents: set[str]) -> Iterator[str]:
    """``a -> b -> c`` 形式のワークフローに現れる、存在しないエージェントの誤りを返す。"""
    for match in _WORKFLOW_PATTERN.finditer(content):
        for agent in _WORKFLOW_ARROW_PATTERN.split(match.group(1)):
            if agent not in valid_agents:
                yield f'{file_name} - ワークフローが存在しないエージェント "{agent}" を参照しています'


def _frontmatter_field_errors(file_name: str, content: str) -> Iterator[str]:
    """frontmatter のハイフン／アンダースコアの綴り違いを返す（frontmatter が無いファイルは対象外）。"""
    for key in extract_frontmatter(content) or {}:
        swapped = key.replace("_", "-")
        if swapped != key and swapped in _KNOWN_FRONTMATTER_FIELDS:
            yield f"{file_name} - frontmatter の {key} は {swapped} の綴り違い（黙って無視される）"


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

    body = _CODE_FENCE_PATTERN.sub("", content)
    has_errors = report_errors(_frontmatter_field_errors(name, content))
    has_errors |= report_errors(_command_reference_errors(name, body, valid_commands))
    has_errors |= report_errors(_agent_reference_errors(name, body, valid_agents))
    warn_count = _warn_skill_references(name, body, valid_skills)
    has_errors |= report_errors(_workflow_reference_errors(name, body, valid_agents))
    return has_errors, warn_count


def validate_commands(
    root_dir: str | Path,
    commands_dir: str | Path,
    agents_dir: str | Path,
    skills_dir: str | Path,
) -> int:
    """コマンド Markdown ファイルの相互参照を検証する。

    誤りは 1 件ずつ stderr へ、ローカルに無い skills 参照の警告は stdout へ書き出し、
    全件合格なら検証したファイル数（警告があればその件数も）を stdout へ表示する。

    Args:
        root_dir: 相対指定の各ディレクトリを解決する基準。
        commands_dir: コマンドディレクトリ。
        agents_dir: エージェントディレクトリ。
        skills_dir: スキルディレクトリ。

    Returns:
        終了コード（0 = 合格、1 = 不合格）。
    """
    # 絶対パスとの結合は右辺がそのまま残るので、相対指定だけが root_dir 基準になる。
    root = Path(root_dir)
    commands_path, agents_path, skills_path = root / commands_dir, root / agents_dir, root / skills_dir
    if not commands_path.exists():
        return report_missing("commands ディレクトリ", commands_path)

    files = list_markdown_files(commands_path)
    valid_commands = {path.stem for path in files}
    valid_agents = {path.stem for path in list_markdown_files(agents_path)}
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
    return validate_commands(args.root_dir, args.commands_dir, args.agents_dir, args.skills_dir)


if __name__ == "__main__":
    raise SystemExit(main())
