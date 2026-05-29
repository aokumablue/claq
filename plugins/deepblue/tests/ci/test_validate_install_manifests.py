"""devgear.ci.validate_install_manifests のテスト。"""

from __future__ import annotations

import json
from pathlib import Path

from devgear.ci.validate_install_manifests import validate_install_manifests


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_validate_install_manifests_skips_when_missing(tmp_path, capsys):
    """マニフェストがない場合はスキップされること。"""
    result = validate_install_manifests(
        repo_root=tmp_path,
        modules_manifest_path=tmp_path / "manifests" / "install-modules.json",
        profiles_manifest_path=tmp_path / "manifests" / "install-profiles.json",
    )
    captured = capsys.readouterr()

    assert result == 0
    assert "検証をスキップします" in captured.out


def test_validate_install_manifests_validates_cross_references(tmp_path, capsys):
    """有効なマニフェストは通過し、件数が報告されること。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "a.js").write_text("a\n", encoding="utf-8")
    (repo_root / "scripts" / "b.js").write_text("b\n", encoding="utf-8")

    manifests_dir = repo_root / "manifests"
    write_json(
        manifests_dir / "install-modules.json",
        {
            "version": 1,
            "modules": [
                {"id": "mod-a", "dependencies": [], "paths": ["scripts/a.js"]},
                {"id": "mod-b", "dependencies": ["mod-a"], "paths": ["scripts/b.js"]},
            ],
        },
    )
    write_json(
        manifests_dir / "install-profiles.json",
        {
            "version": 1,
            "profiles": {
                "core": {"modules": ["mod-a"]},
                "developer": {"modules": ["mod-a", "mod-b"]},
                "security": {"modules": ["mod-a"]},
                "research": {"modules": ["mod-a"]},
                "full": {"modules": ["mod-a", "mod-b"]},
            },
        },
    )
    write_json(
        manifests_dir / "install-components.json",
        {
            "version": 1,
            "components": [
                {"id": "baseline:core", "family": "baseline", "modules": ["mod-a"]},
                {"id": "lang:js", "family": "language", "modules": ["mod-b"]},
            ],
        },
    )

    result = validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=manifests_dir / "install-modules.json",
        profiles_manifest_path=manifests_dir / "install-profiles.json",
        components_manifest_path=manifests_dir / "install-components.json",
    )
    captured = capsys.readouterr()

    assert result == 0
    assert (
        "2 個のインストールモジュール、2 個のインストールコンポーネント、5 個のプロファイルを検証しました"
        in captured.out
    )


def test_validate_install_manifests_rejects_unknown_dependency(tmp_path, capsys):
    """未知のモジュール依存関係は失敗すること。"""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "a.js").write_text("a\n", encoding="utf-8")

    manifests_dir = repo_root / "manifests"
    write_json(
        manifests_dir / "install-modules.json",
        {
            "version": 1,
            "modules": [
                {"id": "mod-a", "dependencies": ["mod-b"], "paths": ["scripts/a.js"]},
            ],
        },
    )
    write_json(
        manifests_dir / "install-profiles.json",
        {
            "version": 1,
            "profiles": {
                "core": {"modules": ["mod-a"]},
                "developer": {"modules": ["mod-a"]},
                "security": {"modules": ["mod-a"]},
                "research": {"modules": ["mod-a"]},
                "full": {"modules": ["mod-a"]},
            },
        },
    )

    result = validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=manifests_dir / "install-modules.json",
        profiles_manifest_path=manifests_dir / "install-profiles.json",
    )
    captured = capsys.readouterr()

    assert result == 1
    assert "不明なモジュール" in captured.err
