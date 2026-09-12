"""version-up.sh のテスト。"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE_SCRIPT = ROOT / "scripts" / "version-up.sh"

# version-up.sh に埋め込まれた Python heredoc（`<<'PY'` … `PY`）の抽出パターン。
_PY_HEREDOC_RE = re.compile(r"(?m)^python3 - .*<<'PY'\n(.*?)\n^PY$", re.DOTALL)


def _extract_script_version_targets() -> frozenset[Path]:
    """version-up.sh が更新するファイルのリポジトリ相対パスを機械抽出する。

    埋め込み Python の ``paths = {...}`` を ``ast`` で読み、各値
    （``repo_root / "a" / "b"`` という ``/`` 連鎖）から文字列リテラルを
    取り出して相対パスへ組み立てる。

    手で複製したリストと突き合わせるためのもので、抽出側がスクリプト本体を
    直接読むため定義がずれた瞬間に食い違いが露見する。

    Returns:
        更新対象のリポジトリ相対パス集合。

    Raises:
        AssertionError: heredoc または ``paths`` 代入を見つけられない場合。
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


def run_script(repo_root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """指定したリポジトリで version-up.sh を実行する。"""
    return subprocess.run(
        ["bash", str(repo_root / "scripts" / "version-up.sh"), *args],
        cwd=repo_root,
        env=os.environ.copy(),
        capture_output=True,
        check=False,
        text=True,
    )


def prepare_repo(tmp_path: Path) -> Path:
    """最小構成のリポジトリを用意する。"""
    repo_root = tmp_path / "repo"
    script_dest = repo_root / "scripts" / "version-up.sh"
    script_dest.parent.mkdir(parents=True)
    shutil.copy2(SOURCE_SCRIPT, script_dest)
    script_dest.chmod(0o755)

    for rel in _VERSION_FILES:
        dest = repo_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)

    return repo_root


def read_versions(repo_root: Path) -> tuple[str, str, str, str]:
    """4つのバージョン値を読む。"""
    pyproject = tomllib.loads((repo_root / "plugins" / "claq" / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((repo_root / "plugins" / "claq" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    init_text = (repo_root / "plugins" / "claq" / "src" / "claq" / "mem" / "__init__.py").read_text(
        encoding="utf-8"
    )
    mem_version = init_text.split('__version__ = "', 1)[1].split('"', 1)[0]
    return (
        pyproject["project"]["version"],
        plugin["version"],
        marketplace["plugins"][0]["version"],
        mem_version,
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


def test_bump_version_updates_all_targets(tmp_path: Path) -> None:
    """4箇所のバージョンを同時に更新できること。"""
    repo_root = prepare_repo(tmp_path)
    current = read_versions(repo_root)[0]
    next_version = _next_patch(current)

    result = run_script(repo_root, ["--version", next_version])

    assert result.returncode == 0, result.stderr
    assert f"[version-up] Updated version to {next_version}" in result.stdout
    assert read_versions(repo_root) == (next_version, next_version, next_version, next_version)


def test_bump_version_rejects_invalid_version(tmp_path: Path) -> None:
    """セマンティックバージョン形式以外を拒否すること。"""
    repo_root = prepare_repo(tmp_path)
    current = read_versions(repo_root)[0]

    result = run_script(repo_root, ["--version", f"{current}-beta"])

    assert result.returncode != 0
    assert "invalid version format" in result.stderr
    assert read_versions(repo_root) == (current, current, current, current)


def test_bump_version_rejects_preexisting_version_drift(tmp_path: Path) -> None:
    """事前に version が不一致なら更新せず失敗すること。"""
    repo_root = prepare_repo(tmp_path)
    current = read_versions(repo_root)[0]
    next_version = _next_patch(current)
    plugin_json = repo_root / "plugins" / "claq" / ".claude-plugin" / "plugin.json"
    plugin_json.write_text(plugin_json.read_text(encoding="utf-8").replace(f'"{current}"', '"0.0.99"', 1), encoding="utf-8")

    result = run_script(repo_root, ["--version", next_version])

    assert result.returncode != 0
    assert "version mismatch before update" in result.stderr
    assert read_versions(repo_root) == (current, "0.0.99", current, current)


def test_bump_version_help_prints_usage(tmp_path: Path) -> None:
    """--help が usage を表示すること。"""
    repo_root = prepare_repo(tmp_path)

    result = run_script(repo_root, ["--help"])

    assert result.returncode == 0, result.stderr
    assert "Usage: bash scripts/version-up.sh --version X.Y.Z" in result.stdout
