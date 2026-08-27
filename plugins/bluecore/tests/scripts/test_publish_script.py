"""`scripts/publish.sh` の配布除外リストが開発専用アーティファクトを網羅することを検証する。

配布ツリーへ「除去済みパスを指す設定ファイル」が残ると、実機監査が誤検出を
起こす。実際 ADR-0006 の「リスク」節が予言したとおり、``tests/`` を除去しつつ
``testpaths=["tests"]`` と ``fail_under=100`` を持つ ``pyproject.toml`` を
同梱していたため、配布ツリーでの ``pytest`` が「coverage 0% で FAIL」という
回帰そっくりの出力を返し、3 ラウンド続けて「テストが消失した」と誤報告された。

散文（ADR・README）は 3 回とも読まれた上で誤検出を止められなかったので、
ここでは除外リストの定義そのものを機械的に固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH_SH = _REPO_ROOT / "scripts" / "publish.sh"
# 配布ツリーへ持ち出してはいけないパスと、その理由。
_REQUIRED_EXCLUSIONS = {
    "plugins/bluecore/tests/": "開発専用。配布物を最小化する（ADR-0006）",
    "plugins/bluecore/pyproject.toml": (
        "除去済みの tests/ を testpaths/fail_under が指すため、"
        "配布ツリーでの pytest が回帰そっくりに失敗する。"
        "plugin assets を含まない wheel の build 設定も同梱しない"
    ),
    "scripts/": "開発・リリース用スクリプト",
    "CLAUDE.md": "開発リポジトリ固有の指示",
    "conftest.py": "pytest 専用",
}


def _filter_repo_exclusions() -> set[str]:
    """publish.sh の `git filter-repo --invert-paths` が除外するパス集合を返す。"""
    text = _PUBLISH_SH.read_text(encoding="utf-8")
    block = re.search(r"git filter-repo\b(.*?)--force", text, re.DOTALL)
    assert block is not None, "publish.sh に git filter-repo ブロックが見つからない"
    assert "--invert-paths" in block.group(1), "--invert-paths が無いと除外ではなく抽出になる"
    return set(re.findall(r"--path\s+(\S+)", block.group(1)))


def test_publish_excludes_all_development_only_artifacts() -> None:
    """開発専用アーティファクトが 1 つ残らず配布除外されていること。"""
    excluded = _filter_repo_exclusions()
    missing = {path: reason for path, reason in _REQUIRED_EXCLUSIONS.items() if path not in excluded}
    assert not missing, f"配布除外から漏れている: {missing}"


def test_publish_does_not_exclude_paths_that_no_longer_exist() -> None:
    """除外リストに実在しないパスが残っていないこと。

    実体が消えた後も除外エントリが残ると、リストが「いま何を配布しないか」の
    正確な記述でなくなり、レビュー時の判断材料として信用できなくなる。
    """
    stale = [path for path in _filter_repo_exclusions() if not (_REPO_ROOT / path).exists()]
    assert not stale, f"実在しないパスが除外リストに残っている: {stale}"


def test_distributed_pyproject_is_the_only_carrier_of_test_config() -> None:
    """`testpaths` / `fail_under` が、配布除外される pyproject.toml にしか無いこと。

    別ファイル（setup.cfg / tox.ini / pytest.ini 等）へ同じ設定が移ると、
    除外リストを 1 つ足しただけでは誤検出の再発を防げなくなる。
    """
    plugin_root = _REPO_ROOT / "plugins" / "bluecore"
    carriers = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in plugin_root.glob("*")
        if path.is_file() and ("testpaths" in path.read_text(encoding="utf-8", errors="ignore"))
    ]
    assert carriers == ["plugins/bluecore/pyproject.toml"]
