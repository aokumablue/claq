"""ハーネス監査のファイル探索・検出補助とリポジトリマーカー定義。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
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


def file_exists(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在するかを確認する。"""
    return Path(root_dir, relative_path).exists()


def file_has_content(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在し、かつ中身が空でないかを確認する。

    存在検査だけを合格条件にすると、ファイルを 0 バイトへ切り詰めても満点が出る
    （実測: `tests/hooks/test_hook_edge_cases.py` を空にしても 58/58）。存在を根拠に
    「テストがある」「ポリシーがある」と採点する以上、中身の消失は不合格でなければ
    ならない（ADR-0014: skip されるゲートはゲートとして機能しない）。

    Args:
        root_dir: 監査対象ルート。
        relative_path: ルートからの相対パス。

    Returns:
        ファイルが存在し、空白のみでない中身を持つなら True。

    Raises:
        例外は発生しません（読み取り失敗は False として扱う）。
    """
    path = Path(root_dir, relative_path)
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def read_text(root_dir: str | Path, relative_path: str) -> str:
    """テキストファイルを UTF-8 で読む。"""
    return Path(root_dir, relative_path).read_text(encoding="utf-8")


def _walk_dir(root_path: Path) -> Iterator[os.DirEntry[str]]:
    """ディレクトリ以下のファイルエントリを再帰走査して返す。

    Args:
        root_path: 走査を開始するディレクトリ。

    Yields:
        シンボリックリンクを辿らないファイルエントリ。
    """
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
    """repo か consumer かを判定する。

    判定材料はプラグイン提供元としての構造（``REPO_CORE_MARKERS`` と
    ``HARNESS_MARKERS``）だけに置く。以前は派生元プラグイン名
    （``package.json`` の ``name``）を見る分岐が残っており、bluecore 自身と
    無関係な名前で repo 判定していた。
    """
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


# consumer モードで「このプラグインが導入済みか」を見るときの探索先。
# 派生元の名前が残っていたため、bluecore の監査が別プラグインの導入を
# 要求していた。
_PLUGIN_NAME = "bluecore"
_PLUGIN_JSON_RELATIVES = (
    Path(".claude") / "plugins" / _PLUGIN_NAME / ".claude-plugin" / "plugin.json",
    Path(".claude") / "plugins" / _PLUGIN_NAME / "plugin.json",
)


def find_plugin_install(root_dir: str | Path) -> str | None:
    """bluecore プラグインのインストール先を探す。

    リポジトリ直下を先に、``HOME`` があればその配下を続けて探す。
    各ルートでは ``.claude-plugin/plugin.json`` を先に見る。
    """
    search_roots = [Path(root_dir)]
    home_dir = os.environ.get("HOME", "")
    if home_dir:
        search_roots.append(Path(home_dir))

    for search_root in search_roots:
        for relative in _PLUGIN_JSON_RELATIVES:
            candidate = search_root / relative
            if candidate.exists():
                return str(candidate)
    return None
