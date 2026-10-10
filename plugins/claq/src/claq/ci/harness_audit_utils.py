"""ハーネス監査のファイル探索・検出補助とリポジトリマーカー定義。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from claq.lib.core_utils import get_home_dir

REPO_CORE_MARKERS = [
    ".claude-plugin/plugin.json",
    "agents",
    "skills",
]
HARNESS_MARKERS = [
    "src/claq/ci/harness_audit.py",
]


def check(
    check_id: str,
    category: str,
    points: int,
    scopes: list[str],
    path: str,
    description: str,
    passed: bool,
    fix: str,
) -> dict[str, Any]:
    """採点項目 1 件を、レポート組み立てが読む辞書にする。"""
    return {
        "id": check_id,
        "category": category,
        "points": points,
        "scopes": scopes,
        "path": path,
        "description": description,
        "pass": passed,
        "fix": fix,
    }


def file_exists(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在するかを確認する。"""
    return Path(root_dir, relative_path).exists()


def file_has_content(root_dir: str | Path, relative_path: str) -> bool:
    """相対パスが存在し、かつ空白以外の中身を持つかを確認する。

    存在を根拠に「テストがある」「ポリシーがある」と採点する以上、0 バイトへの
    切り詰めは不合格でなければならない（docs/adr/verification-scope-release-gates.md:
    skip されるゲートはゲートとして機能しない）。読み取り失敗は False として扱う。
    """
    path = Path(root_dir, relative_path)
    try:
        return path.is_file() and bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeDecodeError):
        return False


def read_text(root_dir: str | Path, relative_path: str) -> str:
    """テキストファイルを UTF-8 で読む。"""
    return Path(root_dir, relative_path).read_text(encoding="utf-8")


_VENDOR_DIR_NAMES = frozenset({".git", ".venv", "venv", "node_modules", "vendor", "__pycache__"})
"""ファイル走査で降りないディレクトリ名。依存ツリー同梱のファイルを除くため。"""


def _walk_dir(root_path: Path) -> Iterator[os.DirEntry[str]]:
    """ディレクトリ以下のファイルエントリを、シンボリックリンクを辿らず再帰走査して返す。

    `_VENDOR_DIR_NAMES` の枝刈りは、依存ツリーへ同梱された third-party の
    ファイルを「このプロジェクトのもの」として採点に数えないため。述語ごとに
    持つと、枝刈りの無い側を経由して `node_modules/dep/a.spec.js` 1 個で
    `consumer-test-suite` を満点通過しうるので、走査の共通経路である本関数へ寄せる。

    読めない（存在しないものを含む）ディレクトリは `OSError` を握り潰して走査から
    落とすだけで、監査レポート全体を落とさない（`file_has_content` と同じ姿勢）。
    """
    stack = [root_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        yield entry
                    elif entry.name not in _VENDOR_DIR_NAMES:
                        stack.append(Path(entry.path))
        except OSError:
            continue


def has_python_tests(root_dir: str | Path, minimum: int) -> bool:
    """pytest / unittest の命名規約（``test_*.py`` / ``*_test.py``）のテストが ``minimum`` 件以上あるかを調べる。

    拡張子だけの ``has_file_with_extension`` では Python のテストを拾えない
    （``.py`` で照合すると全 Python ファイルが一致する）。JS/TS 規約しか見ないと、
    pytest だけを持つリポジトリが `consumer-test-suite` でも
    `consumer-eval-coverage` でも 0 点になる。

    ``minimum`` に既定値を置かないのは、閾値を呼び出し側に必ず宣言させるため。
    ``minimum`` 件に達した時点で打ち切るのは、テストを持つリポジトリを全数え上げ
    しないため（`consumer-test-suite` の判定順の前提）。

    Args:
        root_dir: 走査するルートディレクトリ。
        minimum: 合格に必要なテストファイルの最小件数。呼び出し側は 1 以上を渡す。
            0 以下でも「0 件で True」にはならない（件数を加算した後にしか閾値を見ない）。
    """
    found = 0
    for entry in _walk_dir(Path(root_dir)):
        if entry.name.endswith(".py") and (entry.name.startswith("test_") or entry.name.endswith("_test.py")):
            found += 1
            if found >= minimum:
                return True
    return False


def count_files(root_dir: str | Path, relative_dir: str, extension: str | None) -> int:
    """指定ディレクトリ以下の、``extension`` で終わる（None なら全）ファイル数を数える。"""
    return sum(
        1 for entry in _walk_dir(Path(root_dir, relative_dir)) if extension is None or entry.name.endswith(extension)
    )


def safe_read(root_dir: str | Path, relative_path: str) -> str:
    """読めなければ空文字を返す読み込み。"""
    try:
        return read_text(root_dir, relative_path)
    except OSError:
        return ""


def safe_parse_json(text: str) -> Any | None:
    """JSON を解析する。空文字や不正 JSON は None。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _has_any_file(root_dir: str | Path, relative_paths: Sequence[str]) -> bool:
    """候補パスのどれか 1 つでも存在するかを調べる。"""
    return any(file_exists(root_dir, relative_path) for relative_path in relative_paths)


def has_file_with_extension(root_dir: str | Path, relative_dir: str, extensions: str | Sequence[str]) -> bool:
    """指定拡張子のファイルが 1 つでもあるかを調べる。"""
    suffixes = (extensions,) if isinstance(extensions, str) else tuple(extensions)
    return any(entry.name.endswith(suffixes) for entry in _walk_dir(Path(root_dir, relative_dir)))


def detect_target_mode(root_dir: str | Path) -> str:
    """repo か consumer かを判定する。

    判定材料はプラグイン提供元としての構造（``REPO_CORE_MARKERS`` と
    ``HARNESS_MARKERS``）だけに置く。``package.json`` の ``name`` のような
    プラグイン名で判定すると、claq 自身と無関係な名前で repo 判定しうる。
    """
    if all(file_exists(root_dir, marker) for marker in REPO_CORE_MARKERS) and _has_any_file(root_dir, HARNESS_MARKERS):
        return "repo"
    return "consumer"


_GITLAB_SECURITY_PATTERNS = (
    r"(?mi)^\s*(dependency_scanning|sast|container_scanning|secret_detection|license_scanning)\s*:",
    r"(?mi)^\s*-\s*template:\s*Security/",
    r"(?mi)^\s*template:\s*Security/",
)


def _has_gitlab_security_scanning(root_dir: str | Path) -> bool:
    """GitLab CI に最低限のセキュリティスキャン設定があるかを確認する。"""
    content = safe_read(root_dir, ".gitlab-ci.yml")
    return any(re.search(pattern, content) for pattern in _GITLAB_SECURITY_PATTERNS)


# consumer モードで「このプラグインが導入済みか」を見るときの探索先。
_PLUGIN_NAME = "claq"
# ホストごとの配置。Copilot CLI は `.copilot/installed-plugins/<plugin>/<plugin>/`
# へ展開する。`.claude` だけを見ると、導入済みの Copilot 環境で
# `consumer-plugin-install` が pass=false になり、既に満たされた助言を第 1 位に出す。
_PLUGIN_JSON_RELATIVES = (
    Path(".claude") / "plugins" / _PLUGIN_NAME / ".claude-plugin" / "plugin.json",
    Path(".claude") / "plugins" / _PLUGIN_NAME / "plugin.json",
    Path(".copilot") / "installed-plugins" / _PLUGIN_NAME / _PLUGIN_NAME / ".claude-plugin" / "plugin.json",
    Path(".copilot") / "installed-plugins" / _PLUGIN_NAME / _PLUGIN_NAME / "plugin.json",
)

# マーケットプレイス経由で導入したときの実体配置。ホストは
# `.claude/plugins/cache/<marketplace>/<plugin>/<version>/` へ展開するため、
# 平置きの `.claude/plugins/<plugin>/` だけを見ると正しく導入済みの環境を
# 「未導入」と判定し、`top_actions` の第 1 位に既に満たされた助言が出る。
_PLUGIN_CACHE_GLOBS = (
    f".claude/plugins/cache/*/{_PLUGIN_NAME}/*/.claude-plugin/plugin.json",
    f".copilot/installed-plugins/*/{_PLUGIN_NAME}/*/.claude-plugin/plugin.json",
)


def find_plugin_install(root_dir: str | Path) -> str | None:
    """claq プラグインの ``plugin.json`` の絶対パスを探す。無ければ None。

    リポジトリ直下を先に、ホームディレクトリ配下を続けて探す。ホームの解決は
    `core_utils.get_home_dir()` に委ねる（``HOME`` を直接読むと、``HOME`` を
    持たない Windows で必ずリポジトリ直下しか見えない）。各ルートでは平置き
    レイアウトを先に見て、無ければマーケットプレイス配置を走査する。複数版が
    残っている場合はパスの辞書順で最初のものを返す。呼び出し側は導入有無しか
    見ないため、これは同じ入力に同じ答えを返させるための順序固定であって、
    版の新旧を表さない。
    """
    for search_root in (Path(root_dir), get_home_dir()):
        for relative in _PLUGIN_JSON_RELATIVES:
            candidate = search_root / relative
            if candidate.exists():
                return str(candidate)
        cached = sorted(str(path) for glob in _PLUGIN_CACHE_GLOBS for path in search_root.glob(glob))
        if cached:
            return cached[0]
    return None
