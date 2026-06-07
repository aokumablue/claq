"""ハーネス監査のファイル探索・検出補助とリポジトリマーカー定義。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

REPO_CORE_MARKERS = [
    ".claude-plugin/plugin.json",
    "agents",
    "skills",
]
HARNESS_MARKERS = [
    "src/bluecore/ci/harness_audit.py",
]
COMMAND_PARITY_PAIRS = [
    ("commands/harness.md", ".opencode/commands/harness.md"),
]


def file_exists(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在するかを確認する。"""
    return Path(root_dir, relative_path).exists()


def read_text(root_dir: str | Path, relative_path: str) -> str:
    """テキストファイルを UTF-8 で読む。"""
    return Path(root_dir, relative_path).read_text(encoding="utf-8")


def _walk_dir(root_path: Path):
    """ディレクトリ以下のエントリを再帰的に走査して順次返す。"""
    stack = [root_path]
    while stack:
        current = stack.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    yield entry


def count_files(root_dir: str | Path, relative_dir: str, extension: str | None) -> int:
    """指定ディレクトリ以下のファイル数を数える。"""
    dir_path = Path(root_dir, relative_dir)
    if not dir_path.exists():
        return 0

    count = 0
    for entry in _walk_dir(dir_path):
        if extension is None or entry.name.endswith(extension):
            count += 1
    return count


def safe_read(root_dir: str | Path, relative_path: str) -> str:
    """失敗しても空文字を返す安全な読み込み。"""
    try:
        return read_text(root_dir, relative_path)
    except OSError:
        return ""


def safe_parse_json(text: str) -> Any | None:
    """空文字や不正 JSON を None として扱う。"""
    if not text or not text.strip():
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _has_any_file(root_dir: str | Path, relative_paths: Sequence[str]) -> bool:
    """候補パスのどれか 1 つでも存在するかを調べる。"""
    return any(file_exists(root_dir, relative_path) for relative_path in relative_paths)


def _command_parity_matches(root_dir: str | Path) -> bool:
    """新旧コマンド名のどちらでもパリティが取れているかを確認する。"""
    for primary_path, parity_path in COMMAND_PARITY_PAIRS:
        primary = safe_read(root_dir, primary_path).strip()
        parity = safe_read(root_dir, parity_path).strip()
        if primary and primary == parity:
            return True
    return False


def has_file_with_extension(root_dir: str | Path, relative_dir: str, extensions: str | Sequence[str]) -> bool:
    """指定拡張子のファイルが 1 つでもあるかを調べる。"""
    dir_path = Path(root_dir, relative_dir)
    if not dir_path.exists():
        return False

    allowed = [extensions] if isinstance(extensions, str) else list(extensions)
    for entry in _walk_dir(dir_path):
        if any(entry.name.endswith(extension) for extension in allowed):
            return True
    return False


def detect_target_mode(root_dir: str | Path) -> str:
    """repo か consumer かを判定する。"""
    package_json = safe_parse_json(safe_read(root_dir, "package.json"))
    if isinstance(package_json, dict) and package_json.get("name") == "everything-claude-code":
        return "repo"

    if all(file_exists(root_dir, marker) for marker in REPO_CORE_MARKERS) and _has_any_file(root_dir, HARNESS_MARKERS):
        return "repo"

    return "consumer"


def _has_gitlab_security_scanning(root_dir: str | Path) -> bool:
    """GitLab CI に最低限のセキュリティスキャン設定があるかを確認する。"""
    content = safe_read(root_dir, ".gitlab-ci.yml")
    if not content:
        return False

    patterns = (
        r"(?mi)^\s*(dependency_scanning|sast|container_scanning|secret_detection|license_scanning)\s*:",
        r"(?mi)^\s*-\s*template:\s*Security/",
        r"(?mi)^\s*template:\s*Security/",
    )
    return any(re.search(pattern, content) for pattern in patterns)


def find_plugin_install(root_dir: str | Path) -> str | None:
    """ECC のインストール先を探す。"""
    home_dir = os.environ.get("HOME", "")
    candidates = [
        Path(root_dir) / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json",
        Path(root_dir) / ".claude" / "plugins" / "everything-claude-code" / "plugin.json",
        Path(home_dir) / ".claude" / "plugins" / "everything-claude-code" / ".claude-plugin" / "plugin.json"
        if home_dir
        else None,
        Path(home_dir) / ".claude" / "plugins" / "everything-claude-code" / "plugin.json" if home_dir else None,
    ]

    for candidate in candidates:
        if candidate is not None and candidate.exists():
            return str(candidate)
    return None
