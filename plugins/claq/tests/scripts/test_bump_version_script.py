"""version-up.sh のテスト。"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]
SOURCE_SCRIPT = ROOT / "scripts" / "version-up.sh"

# version-up.sh に埋め込まれた Python heredoc（`<<'PY'` … `PY`）の抽出パターン。
_PY_HEREDOC_RE = re.compile(r"(?m)^python3 - .*<<'PY'\n(.*?)\n^PY$", re.DOTALL)


def _extract_script_version_targets() -> frozenset[Path]:
    """version-up.sh の埋め込み Python の ``paths = {...}`` を ``ast`` で読み、更新対象の相対パスを返す。

    各値（``repo_root / "a" / "b"`` という ``/`` 連鎖）の文字列リテラルを組み立てる。スクリプト本体を
    直接読むので、手で複製した ``_VERSION_FILES`` と定義がずれた瞬間に食い違いが露見する。
    """
    match = _PY_HEREDOC_RE.search(SOURCE_SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, "version-up.sh の Python heredoc を抽出できない"
    targets: set[Path] = set()
    found_assignment = False
    for node in ast.walk(ast.parse(match.group(1))):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "paths" for t in node.targets):
            continue
        assert isinstance(node.value, ast.Dict), "paths が dict リテラルではない"
        found_assignment = True
        for value in node.value.values:
            parts: list[str] = []
            current: ast.expr = value
            while isinstance(current, ast.BinOp) and isinstance(current.op, ast.Div):
                assert isinstance(current.right, ast.Constant), "パス片が文字列リテラルではない"
                parts.append(current.right.value)
                current = current.left
            assert isinstance(current, ast.Name) and current.id == "repo_root", (
                "パスが repo_root 起点ではない"
            )
            targets.add(Path(*reversed(parts)))
    assert found_assignment, "version-up.sh に paths = {...} が見つからない"
    return frozenset(targets)


# `read_versions` がフォーマット別（toml / json / json 入れ子 / Python 定数）に
# 読むため、順序と対応関係を持つタプルとして保持する。スクリプト側の定義との
# 一致は `test_version_files_match_script_targets` が機械的に強制する。
_VERSION_FILES = (
    Path("plugins/claq/pyproject.toml"),
    Path("plugins/claq/.claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path("plugins/claq/src/claq/mem/__init__.py"),
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """version-up.sh と 4 つの更新対象だけを複製した最小リポジトリ。"""
    repo_root = tmp_path / "repo"
    for rel in (Path("scripts/version-up.sh"), *_VERSION_FILES):
        (repo_root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, repo_root / rel)
    return repo_root


def run_script(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """指定したリポジトリで version-up.sh を実行する。"""
    return subprocess.run(
        ["bash", str(repo_root / "scripts" / "version-up.sh"), *args],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )


def read_versions(repo_root: Path) -> tuple[str, str, str, str]:
    """4つのバージョン値を _VERSION_FILES の順で読む。"""
    pyproject, plugin, marketplace, mem_init = ((repo_root / rel).read_text(encoding="utf-8") for rel in _VERSION_FILES)
    return (
        tomllib.loads(pyproject)["project"]["version"],
        json.loads(plugin)["version"],
        json.loads(marketplace)["plugins"][0]["version"],
        mem_init.split('__version__ = "', 1)[1].split('"', 1)[0],
    )


def _next_patch(version: str) -> str:
    """パッチ番号を 1 つ進めた X.Y.Z を返す。"""
    major_minor, patch = version.rsplit(".", 1)
    return f"{major_minor}.{int(patch) + 1}"


def test_version_files_match_script_targets() -> None:
    """テストの更新対象リストが version-up.sh の定義と完全一致すること。

    片側だけ増えると、増えた側のファイルが「一度も bump されない」まま緑で
    通り続ける。抽出器そのものが空を返して表明が空振りする事故を防ぐため、
    件数の下限も同時に確認する。
    """
    extracted = _extract_script_version_targets()
    assert len(extracted) >= 4, f"抽出結果が少なすぎる（抽出器の破損を疑う）: {extracted}"
    assert set(_VERSION_FILES) == extracted


def test_bump_version_updates_all_targets(repo: Path) -> None:
    """4箇所のバージョンを同時に更新できること。"""
    next_version = _next_patch(read_versions(repo)[0])

    result = run_script(repo, "--version", next_version)

    assert result.returncode == 0, result.stderr
    assert f"[version-up] Updated version to {next_version}" in result.stdout
    assert read_versions(repo) == (next_version,) * 4


@pytest.mark.parametrize(
    ("suffix_args", "ok", "message"),
    [
        (["--help"], True, "Usage: bash scripts/version-up.sh --version X.Y.Z"),
        (["--version", "{current}-beta"], False, "invalid version format"),
    ],
    ids=["help-prints-usage", "rejects-non-semver"],
)
def test_bump_version_without_update(repo: Path, suffix_args: list[str], ok: bool, message: str) -> None:
    """--help は usage を stdout へ、セマンティックバージョン以外は拒否を stderr へ出し、どちらも版を変えないこと。"""
    current = read_versions(repo)[0]

    result = run_script(repo, *(arg.format(current=current) for arg in suffix_args))

    assert (result.returncode == 0) is ok, result.stderr
    assert message in (result.stdout if ok else result.stderr)
    assert read_versions(repo) == (current,) * 4


def test_bump_version_rejects_preexisting_version_drift(repo: Path) -> None:
    """事前に version が不一致なら更新せず失敗すること。"""
    current = read_versions(repo)[0]
    plugin_json = repo / _VERSION_FILES[1]
    plugin_json.write_text(plugin_json.read_text(encoding="utf-8").replace(f'"{current}"', '"0.0.99"', 1), encoding="utf-8")

    result = run_script(repo, "--version", _next_patch(current))

    assert result.returncode != 0
    assert "version mismatch before update" in result.stderr
    assert read_versions(repo) == (current, "0.0.99", current, current)
