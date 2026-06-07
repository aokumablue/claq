#!/usr/bin/env python3
"""
プラグイン全体の settings.json を読み込むユーティリティ。

hooks / mem のセクションを、共通の読み取り方法で扱います。
project.coverage は CLAUDE.md から抽出したヒント行を Claude が解釈します。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_SETTINGS_PATH = PLUGIN_ROOT / "settings.json"

# 「カバレッジ」または「coverage」を含む行にマッチ
_COVERAGE_LINE_RE = re.compile(r"^.*(?:カバレッジ|coverage).*$", re.IGNORECASE | re.MULTILINE)


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    """settings.json を読み込み、JSON オブジェクトを返す。

    Args:
        path: 読み込む settings.json のパスです。省略時はプラグイン同梱の settings.json を使います。

    Returns:
        読み込めた JSON オブジェクトを返します。読み込みや解析に失敗した場合は空 dict を返します。

    Raises:
        例外は発生しません。
    """
    settings_path = Path(path).expanduser().resolve() if path is not None else DEFAULT_SETTINGS_PATH

    try:
        raw = settings_path.read_text(encoding="utf-8")
    except OSError:
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}

    return data if isinstance(data, dict) else {}


def get_nested(data: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """ネストした辞書から値を取り出す。

    Args:
        data: 対象の辞書です。
        *keys: たどるキー名です。
        default: 値が見つからない場合の既定値です。

    Returns:
        見つかった値、または default を返します。

    Raises:
        例外は発生しません。
    """
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)

    return default if current is None else current


def get_hook_settings(settings: dict[str, Any], hook_name: str) -> dict[str, Any]:
    """hooks セクションから対象 hook の設定を返す。

    Args:
        settings: settings.json 全体の辞書です。
        hook_name: hooks 配下の名前です。

    Returns:
        hook 設定の辞書、または空 dict を返します。

    Raises:
        例外は発生しません。
    """
    value = get_nested(settings, "hooks", hook_name, default={})
    return value if isinstance(value, dict) else {}


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


