"""スキル作成ツールの端末出力フォーマッタ。"""

from __future__ import annotations

import json
import math
import re
import sys
from typing import Any

from bluecore.lib.git_hosting import (
    detect_git_hosting_service,
    get_git_hosting_review_command,
    get_git_hosting_service_label,
    normalize_git_hosting_service,
)

ANSI_RESET = "\x1b[0m"
ANSI_CODES = {
    "bold": "1",
    "cyan": "36",
    "green": "32",
    "yellow": "33",
    "magenta": "35",
    "gray": "90",
    "white": "37",
    "red": "31",
    "dim": "2",
    "bgCyan": "46",
}

BOX = {
    "topLeft": "╭",
    "topRight": "╮",
    "bottomLeft": "╰",
    "bottomRight": "╯",
    "horizontal": "─",
    "vertical": "│",
}

SPINNER = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")


def _wrap(code: str, value: Any) -> str:
    """ANSI カラーコードで値をラップする。"""
    return f"\x1b[{code}m{value}{ANSI_RESET}"


def bold(value: Any) -> str:
    """太字で装飾する。"""
    return _wrap(ANSI_CODES["bold"], value)


def cyan(value: Any) -> str:
    """シアン色で装飾する。"""
    return _wrap(ANSI_CODES["cyan"], value)


def green(value: Any) -> str:
    """緑色で装飾する。"""
    return _wrap(ANSI_CODES["green"], value)


def yellow(value: Any) -> str:
    """黄色で装飾する。"""
    return _wrap(ANSI_CODES["yellow"], value)


def magenta(value: Any) -> str:
    """マゼンタ色で装飾する。"""
    return _wrap(ANSI_CODES["magenta"], value)


def gray(value: Any) -> str:
    """グレー色で装飾する。"""
    return _wrap(ANSI_CODES["gray"], value)


def white(value: Any) -> str:
    """白色で装飾する。"""
    return _wrap(ANSI_CODES["white"], value)


def red(value: Any) -> str:
    """赤色で装飾する。"""
    return _wrap(ANSI_CODES["red"], value)


def dim(value: Any) -> str:
    """暗色で装飾する。"""
    return _wrap(ANSI_CODES["dim"], value)


def bg_cyan(value: Any) -> str:
    """シアン背景で装飾する。"""
    return _wrap(ANSI_CODES["bgCyan"], value)


def strip_ansi(value: Any) -> str:
    """文字列から ANSI エスケープコードを除去する。"""
    return ANSI_ESCAPE_RE.sub("", str(value))


def _js_round(value: float) -> int:
    """JavaScript 風の丸め処理（0.5 以上を切り上げ）。"""
    return int(math.floor(value + 0.5))


def box(title: Any, content: Any, width: int = 60) -> str:
    """枠付きのボックスを生成する。"""
    lines = str(content).split("\n")
    inner_width = max(0, width - len(str(title)) - 5)
    top = f"{BOX['topLeft']}{BOX['horizontal']} {bold(cyan(title))} {BOX['horizontal'] * inner_width}{BOX['topRight']}"
    bottom = f"{BOX['bottomLeft']}{BOX['horizontal'] * max(0, width - 2)}{BOX['bottomRight']}"
    middle = []
    for line in lines:
        visible = len(strip_ansi(line))
        padding = max(0, width - 4 - visible)
        middle.append(f"{BOX['vertical']} {line}{' ' * padding} {BOX['vertical']}")
    return "\n".join([top, *middle, bottom])


def progress_bar(percent: int, width: int = 30) -> str:
    """プログレスバーを生成する。"""
    filled = min(width, max(0, _js_round(width * percent / 100)))
    empty = width - filled
    bar = green("█" * filled) + gray("░" * empty)
    return f"{bar} {bold(percent)}%"


def render_header(repo_name: Any) -> str:
    """スキル作成ツールのヘッダーを生成する。"""
    subtitle = f"Extracting patterns from {cyan(repo_name)}"
    subtitle_padding = max(0, 59 - len(strip_ansi(subtitle)))
    title_inner = bold("  bluecore Skill Creator".ljust(64))
    return "\n".join(
        [
            "",
            bold(magenta("╔" + "═" * 64 + "╗")),
            bold(magenta("║")) + title_inner + bold(magenta("║")),
            bold(magenta("║")) + f"     {subtitle}{' ' * subtitle_padding}" + bold(magenta("║")),
            bold(magenta("╚" + "═" * 64 + "╝")),
            "",
        ]
    )


def render_analysis_results(data: dict[str, Any]) -> str:
    """解析結果のサマリーを生成する。"""
    content = (
        f"\n{bold('Commits Analyzed:')} {yellow(data.get('commits', ''))}\n"
        f"{bold('Time Range:')}       {gray(data.get('timeRange', ''))}\n"
        f"{bold('Contributors:')}     {cyan(data.get('contributors', ''))}\n"
        f"{bold('Files Tracked:')}    {green(data.get('files', ''))}\n"
    )
    return "\n" + box("Analysis Results", content) + "\n"


def render_patterns(items: Any) -> str:
    """発見されたパターンをフォーマットして表示する。"""
    patterns = list(items or [])
    lines = ["", bold(cyan("Key Patterns Discovered:")), gray("─" * 50)]

    for index, pattern in enumerate(patterns):
        confidence_value = pattern.get("confidence")
        if confidence_value is None:
            confidence_value = 0.8
        confidence_bar = progress_bar(_js_round(float(confidence_value) * 100), 15)
        lines.extend(
            [
                "",
                f"  {bold(yellow(f'{index + 1}.'))} {bold(pattern.get('name', ''))}",
                f"     {gray('Trigger:')} {pattern.get('trigger', '')}",
                f"     {gray('Confidence:')} {confidence_bar}",
                f"     {dim(pattern.get('evidence', ''))}",
            ]
        )

    lines.append("")
    return "\n".join(lines)


def render_instincts(items: Any) -> str:
    """生成された本能をフォーマットして表示する。"""
    instincts = list(items or [])
    lines = []
    for index, instinct in enumerate(instincts):
        confidence_value = instinct.get("confidence", 0)
        percent = _js_round(float(confidence_value) * 100)
        lines.append(f"{yellow(f'{index + 1}.')} {bold(instinct.get('name', ''))} {gray(f'({percent}%)')}")
    content = "\n".join(lines)
    return "\n" + box("Instincts Generated", content) + "\n"


def render_output(skill_path: Any, instincts_path: Any) -> str:
    """生成されたスキルファイルと本能ファイルのパスを表示する。"""
    return "\n".join(
        [
            "",
            bold(green("Generation Complete!")),
            gray("─" * 50),
            "",
            f"  {green('-')} {bold('Skill File:')}",
            f"     {cyan(skill_path or '')}",
            "",
            f"  {green('-')} {bold('Instincts File:')}",
            f"     {cyan(instincts_path or '')}",
            "",
        ]
    )


def render_next_steps() -> str:
    """次のステップを案内するメッセージを表示する。"""
    content = (
        f"\n{yellow('1.')} Review the generated SKILL.md\n"
        f"{yellow('2.')} Import instincts: {cyan('/instinct-import <path>')}\n"
        f"{yellow('3.')} View learned patterns: {cyan('/instinct-status')}\n"
        f"{yellow('4.')} Evolve into skills: {cyan('/instinct evolve')}\n"
    )
    return box("Next Steps", content) + "\n"


def render_footer(service: str | None = None) -> str:
    """フッター情報を表示する。"""
    hosting_service = normalize_git_hosting_service(service or detect_git_hosting_service())
    hosting_label = get_git_hosting_service_label(hosting_service)

    footer_line = f"  {hosting_label} CLI: {get_git_hosting_review_command(hosting_service)}"

    return "\n".join(
        [
            gray("─" * 60),
            dim("  Powered by bluecore • bluecore.tools"),
            dim(footer_line),
            "",
        ]
    )


def render_analyze_phase(data: dict[str, Any]) -> str:
    """分析フェーズの進捗を表示する。"""
    commits = data.get("commits", 0)
    steps = [
        "Parsing git history...",
        f"Found {yellow(commits)} commits",
        "Analyzing commit patterns...",
        "Detecting file co-changes...",
        "Identifying workflows...",
        "Extracting architecture patterns...",
    ]

    lines = ["", f"{cyan('[RUN]')} Analyzing Repository..."]
    for index, step in enumerate(steps):
        lines.append(f"   {gray(SPINNER[index % len(SPINNER)])} {step}")
        lines.append(f"   {green('[DONE]')} {step}")
    lines.append("")
    return "\n".join(lines)


HELP_TEXT = """\
Skill Creator Output Formatter

Usage:
  python -m bluecore.skill_create_output [command]

Commands:
  header            Render the header block
  analysis-results  Render analysis summary output (reads JSON from stdin)
  patterns          Render discovered patterns (reads JSON array from stdin)
  instincts         Render instincts output (reads JSON array from stdin)
  output            Render the completion block (reads JSON object from stdin)
  next-steps        Render the next steps block
  footer            Render the footer block
  analyze-phase     Render the analysis phase transcript (reads JSON from stdin)
"""


def _read_json_stdin(default: Any = None) -> Any:
    """標準入力から JSON を読み取る。空入力の場合は default を返す。"""
    raw = sys.stdin.read()
    if not raw.strip():
        return default
    return json.loads(raw)


def _dispatch_command(command: str, args: list[str]) -> None:
    """コマンド名に対応するレンダラーを呼び出し、結果を stdout に書き出す。

    未知のコマンドの場合は ValueError を送出する。
    """
    if command == "header":
        sys.stdout.write(render_header(args[1] if len(args) > 1 else ""))
    elif command == "analysis-results":
        sys.stdout.write(render_analysis_results(_read_json_stdin({})))
    elif command == "patterns":
        sys.stdout.write(render_patterns(_read_json_stdin([])))
    elif command == "instincts":
        sys.stdout.write(render_instincts(_read_json_stdin([])))
    elif command == "output":
        payload = _read_json_stdin({})
        sys.stdout.write(render_output(payload.get("skillPath"), payload.get("instinctsPath")))
    elif command == "next-steps":
        sys.stdout.write(render_next_steps())
    elif command == "footer":
        sys.stdout.write(render_footer())
    elif command == "analyze-phase":
        sys.stdout.write(render_analyze_phase(_read_json_stdin({})))
    else:
        raise ValueError(f"Unknown command: {command}")


def main(argv: list[str] | None = None) -> int:
    """CLI のエントリポイント。"""
    args = list(sys.argv[1:] if argv is None else argv)

    if not args or args[0] in {"-h", "--help"}:
        sys.stdout.write(HELP_TEXT)
        return 0

    try:
        _dispatch_command(args[0], args)
        return 0
    except (json.JSONDecodeError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
