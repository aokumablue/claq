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
    (repo_root / "scripts").mkdir(parents=True)
    (repo_root / "plugins" / "devgear" / ".claude-plugin").mkdir(parents=True)
    (repo_root / "plugins" / "devgear" / "src" / "devgear" / "mem").mkdir(parents=True)
    (repo_root / ".claude-plugin").mkdir(parents=True)

    shutil.copy2(SOURCE_SCRIPT, repo_root / "scripts" / "version-up.sh")
    (repo_root / "scripts" / "version-up.sh").chmod(0o755)

    shutil.copy2(ROOT / "plugins" / "devgear" / "pyproject.toml", repo_root / "plugins" / "devgear" / "pyproject.toml")
    shutil.copy2(
        ROOT / "plugins" / "devgear" / ".claude-plugin" / "plugin.json",
        repo_root / "plugins" / "devgear" / ".claude-plugin" / "plugin.json",
    )
    shutil.copy2(
        ROOT / ".claude-plugin" / "marketplace.json",
        repo_root / ".claude-plugin" / "marketplace.json",
    )
    shutil.copy2(
        ROOT / "plugins" / "devgear" / "src" / "devgear" / "mem" / "__init__.py",
        repo_root / "plugins" / "devgear" / "src" / "devgear" / "mem" / "__init__.py",
    )

    return repo_root


def read_versions(repo_root: Path) -> tuple[str, str, str, str]:
    """4つのバージョン値を読む。"""
    return (
        tomllib.loads((repo_root / "plugins" / "devgear" / "pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ],
        json.loads((repo_root / "plugins" / "devgear" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))[
            "version"
        ],
        json.loads((repo_root / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))["plugins"][0][
            "version"
        ],
        (repo_root / "plugins" / "devgear" / "src" / "devgear" / "mem" / "__init__.py")
        .read_text(encoding="utf-8")
        .split('__version__ = "', 1)[1]
        .split('"', 1)[0],
    )


def test_bump_version_updates_all_targets(tmp_path: Path) -> None:
    """4箇所のバージョンを同時に更新できること。"""
    repo_root = prepare_repo(tmp_path)
    current = read_versions(repo_root)[0]
    next_version = f"{current.rsplit('.', 1)[0]}.{int(current.rsplit('.', 1)[1]) + 1}"

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
    next_version = f"{current.rsplit('.', 1)[0]}.{int(current.rsplit('.', 1)[1]) + 1}"
    plugin_json = repo_root / "plugins" / "devgear" / ".claude-plugin" / "plugin.json"
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
