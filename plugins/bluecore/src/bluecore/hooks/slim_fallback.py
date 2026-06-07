"""output-styles 非対応環境（GitHub Copilot 等）向けの slim フォールバック注入。

Claude Code では slim は output-style（force-for-plugin）として自動適用されるため、
このフック注入は不要。CLAUDECODE 環境変数の有無で環境を判定する。
"""

from __future__ import annotations

import os
from pathlib import Path

from bluecore.lib.core_utils import log
from bluecore.lib.sanitize import sanitize_log_value

_SLIM_STYLE_PATH = Path(__file__).parents[3] / "output-styles" / "slim.md"


def output_styles_supported() -> bool:
    """output-styles が効く環境（Claude Code）かを返す。

    Claude Code は CLAUDECODE 環境変数を設定するが、GitHub Copilot 等は設定しない
    （判定シグナルは cli_runner.detect_cli_binary と同一）。
    """
    return bool(os.environ.get("CLAUDECODE"))


def strip_frontmatter(text: str) -> str:
    """先頭の YAML frontmatter（--- ... ---）を除いた本文を返す。

    frontmatter が無い・閉じが無い場合は元のテキストをそのまま返す。
    """
    lines = text.split("\n")
    if lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    return text


def inject_slim_skill() -> list[str]:
    """output-styles 非対応環境向けに slim 本文をフォールバック注入する。

    Claude Code では output-style が適用されるため注入しない。文言は
    output-styles/slim.md を単一の真実源として読み込み、frontmatter を除いて返す。
    """
    if output_styles_supported():
        return []
    try:
        if _SLIM_STYLE_PATH.exists():
            return [strip_frontmatter(_SLIM_STYLE_PATH.read_text(encoding="utf-8")).strip()]
    except Exception as e:
        log(f"[SessionStart] Slim injection error: {sanitize_log_value(str(e))}")
    return []
