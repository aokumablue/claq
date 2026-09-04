"""ハーネス監査のファイル探索・検出補助とリポジトリマーカー定義。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from ple4.lib.core_utils import get_home_dir

REPO_CORE_MARKERS = [
    ".claude-plugin/plugin.json",
    "agents",
    "skills",
]
HARNESS_MARKERS = [
    "src/ple4/ci/harness_audit.py",
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


_VENDOR_DIR_NAMES = frozenset({".git", ".venv", "venv", "node_modules", "vendor", "__pycache__"})
"""テスト検出時に降りないディレクトリ名。依存ツリー同梱のテストを除くため。"""


def has_python_tests(root_dir: str | Path, minimum: int) -> bool:
    """pytest / unittest の命名規約に沿ったテストファイルが ``minimum`` 件以上あるかを調べる。

    拡張子だけで判定する ``has_file_with_extension`` では Python のテストを
    拾えない。規約が接頭辞（``test_*.py``）と接尾辞（``*_test.py``）に割れて
    おり、``.py`` で照合すると全 Python ファイルが一致してしまうためです。
    JS/TS 規約しか見ていなかったころは、pytest だけを持つ Python
    リポジトリが `consumer-test-suite`（1 件以上）でも
    `consumer-eval-coverage`（複数件）でも 0 点になり「テストを追加せよ」と
    助言されていました（実測: 2,466 件の pytest を持つ本リポジトリでも
    `evals/` が無ければ後者が 0 点）。

    ``minimum`` に既定値を置かないのは、閾値を呼び出し側に必ず宣言させる
    ためです。既定値つきの引数は「1 件でよい」旧挙動を暗黙に温存する経路に
    なり、閾値の異なる 2 つの検査を同じ関数へ寄せた意味を失います。

    ``minimum`` 件に達した時点で打ち切るのは、`consumer-test-suite` の
    判定順コメントが定める性能契約（テストを持つリポジトリを全数え上げ
    しない）を数え上げ版でも守るためです。

    `_VENDOR_DIR_NAMES` を枝刈りするのは、依存ツリーへ同梱された
    third-party のテスト（`.venv/lib/**/test_*.py` 等）を「このプロジェクトの
    テスト」として加点しないためです。読めないディレクトリは走査から
    落とすだけで、監査レポート全体を落とさない（`file_has_content` と
    同じ姿勢）。

    Args:
        root_dir: 走査するルートディレクトリ。
        minimum: 合格に必要なテストファイルの最小件数。呼び出し側は 1 以上を渡す。
            検証はせず、0 以下でも「0 件で True」にはならない（件数を加算した
            後にしか閾値を見ないため）。

    Returns:
        プロジェクト自身のテストファイルが ``minimum`` 件以上あれば True。

    Raises:
        例外は発生しません（`OSError` は該当ディレクトリのスキップとして扱う）。
    """
    stack = [Path(root_dir)]
    found = 0
    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in _VENDOR_DIR_NAMES:
                            stack.append(Path(entry.path))
                    elif entry.name.endswith(".py") and (
                        entry.name.startswith("test_") or entry.name.endswith("_test.py")
                    ):
                        found += 1
                        if found >= minimum:
                            return True
        except OSError:
            continue
    return False


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
    （``package.json`` の ``name``）を見る分岐が残っており、ple4 自身と
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
# 派生元の名前が残っていたため、ple4 の監査が別プラグインの導入を
# 要求していた。
_PLUGIN_NAME = "ple4"
# ホストごとの配置。Copilot CLI は `.copilot/installed-plugins/<plugin>/<plugin>/`
# へ展開する（release-verify 2026-09-03 の P1-007。実インストール先の実測）。
# `.claude` だけを見ていた頃は、導入済みの Copilot 環境で
# `consumer-plugin-install` が pass=false になり、監査が「既に満たされた助言」を
# 第 1 位に出していた。
_PLUGIN_JSON_RELATIVES = (
    Path(".claude") / "plugins" / _PLUGIN_NAME / ".claude-plugin" / "plugin.json",
    Path(".claude") / "plugins" / _PLUGIN_NAME / "plugin.json",
    Path(".copilot") / "installed-plugins" / _PLUGIN_NAME / _PLUGIN_NAME / ".claude-plugin" / "plugin.json",
    Path(".copilot") / "installed-plugins" / _PLUGIN_NAME / _PLUGIN_NAME / "plugin.json",
)

# マーケットプレイス経由で導入したときの実体配置。ホストは
# `.claude/plugins/cache/<marketplace>/<plugin>/<version>/` へ展開するため、
# 平置きの `.claude/plugins/<plugin>/` だけを見ると正しく導入済みの環境を
# 「未導入」と判定する（実測: `~/.claude/plugins/cache/ple4/ple4/0.9.48/`
# へ導入済みの機で `consumer-plugin-install` が pass=false になり、
# `top_actions` の第 1 位に「プラグインを導入せよ」という既に満たされた
# 助言が出た）。
_PLUGIN_CACHE_GLOBS = (
    f".claude/plugins/cache/*/{_PLUGIN_NAME}/*/.claude-plugin/plugin.json",
    f".copilot/installed-plugins/*/{_PLUGIN_NAME}/*/.claude-plugin/plugin.json",
)


def find_plugin_install(root_dir: str | Path) -> str | None:
    """ple4 プラグインのインストール先を探す。

    リポジトリ直下を先に、ホームディレクトリ配下を続けて探す。ホームの解決は
    `core_utils.get_home_dir()` に委ねる（``HOME`` を直接読んでいた頃は、
    ``HOME`` を持たない Windows で必ずリポジトリ直下しか見えなかった。
    P1-007）。各ルートでは平置きレイアウト（``.claude/plugins/ple4/`` /
    ``.copilot/installed-plugins/ple4/ple4/``）を先に見て、見つからなければ
    マーケットプレイス配置（``<host>/.../<marketplace>/ple4/<version>/``）を
    走査する。同じルートに複数版が残っている場合はパスの**辞書順で最初**の
    ものを返す。呼び出し側は導入有無しか見ないため、これは同じ入力に同じ答えを
    返させるための順序固定であって、版の新旧を表さない（辞書順では
    ``0.9.48`` より ``0.9.9`` が後ろに来る）。

    Args:
        root_dir: 監査対象のルートディレクトリ。

    Returns:
        見つかった ``plugin.json`` の絶対パス文字列。無ければ None。

    Raises:
        例外は発生しません。
    """
    search_roots = [Path(root_dir), get_home_dir()]

    for search_root in search_roots:
        for relative in _PLUGIN_JSON_RELATIVES:
            candidate = search_root / relative
            if candidate.exists():
                return str(candidate)
        cached = sorted(
            str(path) for glob in _PLUGIN_CACHE_GLOBS for path in search_root.glob(glob)
        )
        if cached:
            return cached[0]
    return None
