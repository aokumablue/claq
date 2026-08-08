#!/usr/bin/env python3
"""
プラグイン全体の settings.json を読み込むユーティリティ。

hooks / mem のセクションを、共通の読み取り方法で扱います。
project.coverage は CLAUDE.md から抽出したヒント行を Claude が解釈します。
"""

from __future__ import annotations

import re
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SETTINGS_PATH = PLUGIN_ROOT / "settings.json"

# 「カバレッジ」または「coverage」を含む行にマッチ
_COVERAGE_LINE_RE = re.compile(r"^.*(?:カバレッジ|coverage).*$", re.IGNORECASE | re.MULTILINE)


def extract_coverage_hint_lines(cwd: str | Path | None = None) -> str:
    """CLAUDE.md からカバレッジ関連行を原文のまま返す。

    抽出した行テキストを Claude のコンテキストに渡し、AI 側で目標率を解釈させる。
    対象: `{cwd}/CLAUDE.md` → `{cwd}/.claude/CLAUDE.md`。

    Args:
        cwd: 検索起点のディレクトリ。省略時はカレントディレクトリです。

    Returns:
        マッチした行を改行結合した文字列。見つからなければ空文字列。

    Raises:
        例外は発生しません。
    """
    base = Path(cwd).expanduser().resolve() if cwd is not None else Path.cwd()
    candidates = [base / "CLAUDE.md", base / ".claude" / "CLAUDE.md"]
    for path in candidates:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = [m.group(0) for m in _COVERAGE_LINE_RE.finditer(text)]
        if lines:
            return "\n".join(lines)
    return ""


