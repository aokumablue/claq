"""version-up.sh のテスト。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SOURCE_SCRIPT = ROOT / "scripts" / "version-up.sh"
_VERSION_FILES = (
    Path("plugins/ple4/pyproject.toml"),
    Path("plugins/ple4/.claude-plugin/plugin.json"),
    Path(".claude-plugin/marketplace.json"),
    Path("plugins/ple4/src/ple4/mem/__init__.py"),
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
    pyproject = tomllib.loads((repo_root / "plugins" / "ple4" / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = json.loads((repo_root / "plugins" / "ple4" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    marketplace = json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    init_text = (repo_root / "plugins" / "ple4" / "src" / "ple4" / "mem" / "__init__.py").read_text(
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
    plugin_json = repo_root / "plugins" / "ple4" / ".claude-plugin" / "plugin.json"
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
