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

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PUBLISH_SH = _REPO_ROOT / "scripts" / "publish.sh"
# 配布ツリーへ持ち出してはいけないパスと、その理由。
_REQUIRED_EXCLUSIONS = {
    "plugins/claq/tests/": "開発専用。配布物を最小化する（ADR-0006）",
    "plugins/claq/pyproject.toml": (
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


# 配布ツリーの pytest を「回帰そっくりに失敗」させうる設定キー。
#
# `testpaths` だけを走査していた頃は、``setup.cfg`` に
# ``[coverage:report] fail_under = 100`` だけを置けば本テストが緑のまま通った
# （かつ setup.cfg は publish.sh の除外 5 件に含まれない）。docstring が
# 「両方を守る」と宣言しながら片方しか見ていない状態だったため、宣言側では
# なく走査側をキーの列挙へ揃える。`addopts` も同じ経路で
# ``--cov`` / ``--cov-fail-under`` を持ち込めるため含める。
_TEST_CONFIG_KEYS = ("testpaths", "fail_under", "addopts")


@pytest.mark.parametrize("key", _TEST_CONFIG_KEYS)
def test_distributed_pyproject_is_the_only_carrier_of_test_config(key: str) -> None:
    """テスト設定キーが、配布除外される pyproject.toml にしか無いこと。

    別ファイル（setup.cfg / tox.ini / pytest.ini 等）へ同じ設定が移ると、
    除外リストを 1 つ足しただけでは誤検出の再発を防げなくなる。キーごとに
    独立ケースとして回し、1 キーの漏れが他キーの一致に紛れないようにする。
    """
    plugin_root = _REPO_ROOT / "plugins" / "claq"
    carriers = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in plugin_root.glob("*")
        if path.is_file() and (key in path.read_text(encoding="utf-8", errors="ignore"))
    )
    assert carriers == ["plugins/claq/pyproject.toml"]
