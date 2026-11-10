"""version-up.sh と bump-*.sh のテスト。"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "scripts" / "version-up.sh"

# version-up.sh に埋め込まれた Python heredoc（`<<'PY'` … `PY`）の抽出パターン。
_PY_HEREDOC_RE = re.compile(r"(?m)^python3 - .*<<'PY'\n(.*?)\n^PY$", re.DOTALL)


def _relative_path(expr: ast.expr) -> Path:
    """``repo_root / "a" / "b"`` という ``/`` 連鎖の文字列リテラルを、repo_root 相対の Path へ組み立てる。"""
    parts: list[str] = []
    while isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div):
        assert isinstance(expr.right, ast.Constant), "パス片が文字列リテラルではない"
        parts.append(expr.right.value)
        expr = expr.left
    assert isinstance(expr, ast.Name), "パスが repo_root 起点ではない"
    assert expr.id == "repo_root", "パスが repo_root 起点ではない"
    return Path(*reversed(parts))


def _assigns_paths(node: ast.AST) -> bool:
    """node が ``paths`` への代入文かを返す。"""
    return isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "paths" for t in node.targets)


def _extract_script_version_targets() -> frozenset[Path]:
    """version-up.sh の埋め込み Python の ``paths = {...}`` を ``ast`` で読み、更新対象の相対パスを返す。

    スクリプト本体を直接読むので、手で複製した ``_VERSION_FILES`` と定義がずれた瞬間に食い違いが露見する。
    """
    match = _PY_HEREDOC_RE.search(_SCRIPT.read_text(encoding="utf-8"))
    assert match is not None, "version-up.sh の Python heredoc を抽出できない"
    assignments = [node for node in ast.walk(ast.parse(match.group(1))) if _assigns_paths(node)]
    assert assignments, "version-up.sh に paths = {...} が見つからない"
    targets: set[Path] = set()
    for assignment in assignments:
        assert isinstance(assignment.value, ast.Dict), "paths が dict リテラルではない"
        targets.update(_relative_path(value) for value in assignment.value.values)
    return frozenset(targets)


# `_read_versions` がフォーマット別（toml / json / json 入れ子）に
# 読むため、順序と対応関係を持つタプルとして保持する。スクリプト側の定義との
# 一致は `test_version_files_match_script_targets` が機械的に強制する。
_VERSION_FILES = (
    Path("plugins/claq/pyproject.toml"),
    Path("plugins/claq/.claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """version-up.sh・bump-*.sh と 3 つの更新対象だけを複製した最小リポジトリ。"""
    repo_root = tmp_path / "repo"
    bump_scripts = (Path(f"scripts/bump-{part}.sh") for part in ("major", "minor", "patch"))
    for rel in (Path("scripts/version-up.sh"), *bump_scripts, *_VERSION_FILES):
        (repo_root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(_REPO_ROOT / rel, repo_root / rel)
    return repo_root


def _run_script(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """指定したリポジトリで version-up.sh を実行する。"""
    return subprocess.run(
        ["bash", str(repo_root / "scripts" / "version-up.sh"), *args],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )


def _read_versions(repo_root: Path) -> tuple[str, str, str]:
    """3つのバージョン値を _VERSION_FILES の順で読む。"""
    pyproject, plugin, marketplace = ((repo_root / rel).read_text(encoding="utf-8") for rel in _VERSION_FILES)
    return (
        tomllib.loads(pyproject)["project"]["version"],
        json.loads(plugin)["version"],
        json.loads(marketplace)["plugins"][0]["version"],
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
    assert len(extracted) >= 3, f"抽出結果が少なすぎる（抽出器の破損を疑う）: {extracted}"
    assert set(_VERSION_FILES) == extracted


def test_bump_version_updates_all_targets(repo: Path) -> None:
    """3箇所のバージョンを同時に更新できること。"""
    next_version = _next_patch(_read_versions(repo)[0])

    result = _run_script(repo, "--version", next_version)

    assert result.returncode == 0, result.stderr
    assert f"[version-up] Updated version to {next_version}" in result.stdout
    assert _read_versions(repo) == (next_version,) * 3


@pytest.mark.parametrize(
    ("suffix_args", "ok", "message"),
    [
        (["--help"], True, "Usage: bash scripts/version-up.sh (--version X.Y.Z | --bump major|minor|patch)"),
        (["--version", "{current}-beta"], False, "invalid version format"),
        (["--bump", "build"], False, "invalid bump part: build"),
        (["--bump", "patch", "--version", "9.9.9"], False, "--version and --bump are mutually exclusive"),
        ([], False, "--version or --bump is required"),
    ],
    ids=[
        "help-prints-usage", "rejects-non-semver", "rejects-unknown-bump-part",
        "rejects-both-modes", "requires-a-mode",
    ],
)
def test_bump_version_without_update(repo: Path, suffix_args: list[str], ok: bool, message: str) -> None:
    """--help は usage を stdout へ、セマンティックバージョン以外は拒否を stderr へ出し、どちらも版を変えないこと。"""
    current = _read_versions(repo)[0]

    result = _run_script(repo, *(arg.format(current=current) for arg in suffix_args))

    assert (result.returncode == 0) is ok, result.stderr
    assert message in (result.stdout if ok else result.stderr)
    assert _read_versions(repo) == (current,) * 3


def test_bump_version_rejects_preexisting_version_drift(repo: Path) -> None:
    """事前に version が不一致なら更新せず失敗すること。"""
    current = _read_versions(repo)[0]
    plugin_json = repo / _VERSION_FILES[1]
    drifted = plugin_json.read_text(encoding="utf-8").replace(f'"{current}"', '"0.0.99"', 1)
    plugin_json.write_text(drifted, encoding="utf-8")

    result = _run_script(repo, "--version", _next_patch(current))

    assert result.returncode != 0
    assert "version mismatch before update" in result.stderr
    assert _read_versions(repo) == (current, "0.0.99", current)


@pytest.mark.parametrize(
    ("part", "expected"),
    [("major", "{x}.0.0"), ("minor", "{major}.{y}.0"), ("patch", "{major}.{minor}.{z}")],
)
def test_bump_script_increments_the_named_part(repo: Path, part: str, expected: str) -> None:
    """bump-<part>.sh は現在の版の該当部を 1 つ進め、下位を 0 に戻して 3 箇所を更新すること。"""
    major, minor, patch = (int(n) for n in _read_versions(repo)[0].split("."))
    next_version = expected.format(major=major, minor=minor, x=major + 1, y=minor + 1, z=patch + 1)

    result = subprocess.run(
        ["bash", str(repo / "scripts" / f"bump-{part}.sh")], capture_output=True, check=False, text=True
    )

    assert result.returncode == 0, result.stderr
    assert _read_versions(repo) == (next_version,) * 3
