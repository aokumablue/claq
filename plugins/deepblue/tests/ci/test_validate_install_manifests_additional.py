"""devgear.ci.validate_install_manifests の追加テスト。"""

from __future__ import annotations

import importlib
import json
import runpy
import sys
from pathlib import Path

import pytest

from devgear.ci.validate_install_manifests import validate_install_manifests

validate_install_manifests_module = importlib.import_module("devgear.ci.validate_install_manifests")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")


def test_validate_install_manifests_reports_object_and_list_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifests = repo_root / "manifests"
    write_json(manifests / "install-modules.json", [{"id": "x"}])
    write_json(manifests / "install-profiles.json", {"profiles": {}})

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=manifests / "install-modules.json",
        profiles_manifest_path=manifests / "install-profiles.json",
    ) == 1
    assert "install-modules.json はオブジェクトである必要があります" in capsys.readouterr().err

    write_json(manifests / "install-modules.json", {"modules": []})
    write_json(manifests / "install-profiles.json", [])
    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=manifests / "install-modules.json",
        profiles_manifest_path=manifests / "install-profiles.json",
    ) == 1
    assert "install-profiles.json はオブジェクトである必要があります" in capsys.readouterr().err


def test_validate_install_manifests_covers_invalid_entries_and_cross_refs(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "a.js").write_text("a\n", encoding="utf-8")
    manifests = repo_root / "manifests"
    write_json(
        manifests / "install-modules.json",
        {
            "version": 1,
            "modules": [
                "bad",
                {"id": "", "dependencies": [], "paths": []},
                {"id": "mod-a", "dependencies": "bad", "paths": "bad"},
                {
                    "id": "mod-a",
                    "dependencies": ["mod-a", "missing", None],
                    "paths": ["scripts/a.js", "scripts/shared.js", ""],
                },
                {"id": "mod-b", "dependencies": [], "paths": ["scripts/shared.js"]},
            ],
        },
    )
    write_json(
        manifests / "install-profiles.json",
        {
            "version": 1,
            "profiles": {
                "core": "bad",
                "developer": {"modules": "bad"},
                "security": {},
                "research": {"modules": ["mod-a", "mod-a"]},
                "full": {"modules": ["mod-a"]},
            },
        },
    )
    write_json(
        manifests / "install-components.json",
        {
            "version": 1,
            "components": [
                "bad",
                {"id": "baseline:core", "family": "baseline", "modules": ["mod-a"]},
                {"id": "baseline:core", "family": "", "modules": "bad"},
            ],
        },
    )

    assert (
        validate_install_manifests(
            repo_root=repo_root,
            modules_manifest_path=manifests / "install-modules.json",
            profiles_manifest_path=manifests / "install-profiles.json",
            components_manifest_path=manifests / "install-components.json",
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert "モジュールエントリはオブジェクトではありません" in stderr
    assert "モジュールエントリの id が不足しているか無効です" in stderr
    assert "モジュール mod-a の dependencies 配列が無効です" in stderr
    assert "モジュール mod-a は自分自身に依存できません" in stderr
    assert "モジュール mod-a は不明なモジュール missing に依存しています" in stderr
    assert "モジュール mod-a は存在しないパスを参照しています: scripts/shared.js" in stderr
    assert "インストールパス scripts/shared.js は mod-a と mod-b の両方で宣言されています" in stderr
    assert "モジュール mod-a は存在しないパスを参照しています: " in stderr
    assert "必須のインストールプロファイルがありません: full" not in stderr
    assert "プロファイル core はオブジェクトである必要があります" in stderr
    assert "プロファイル developer の modules は配列である必要があります" in stderr
    assert "プロファイル full は不明なモジュール mod-b を参照しています" not in stderr
    assert "full プロファイルにモジュール mod-b がありません" in stderr
    assert "コンポーネントエントリはオブジェクトではありません" in stderr
    assert "コンポーネント baseline:core の family が不足しているか無効です" in stderr
    assert "コンポーネント baseline:core は想定される baseline のプレフィックス baseline: と一致しません" not in stderr


def test_validate_install_manifests_reports_invalid_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifests = repo_root / "manifests"
    modules_path = manifests / "install-modules.json"
    profiles_path = manifests / "install-profiles.json"

    modules_path.parent.mkdir(parents=True, exist_ok=True)
    modules_path.write_text("{", encoding="utf-8")
    write_json(
        profiles_path,
        {
            "version": 1,
            "profiles": {
                "core": {"modules": []},
                "developer": {"modules": []},
                "security": {"modules": []},
                "research": {"modules": []},
                "full": {"modules": []},
            },
        },
    )

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=modules_path,
        profiles_manifest_path=profiles_path,
    ) == 1
    assert "install-modules.json の JSON 形式が不正です" in capsys.readouterr().err


def test_validate_install_manifests_main_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "a.js").write_text("a\n", encoding="utf-8")
    manifests = repo_root / "manifests"
    write_json(
        manifests / "install-modules.json",
        {"version": 1, "modules": [{"id": "mod-a"}]},
    )
    write_json(
        manifests / "install-profiles.json",
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

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=manifests / "install-modules.json",
        profiles_manifest_path=manifests / "install-profiles.json",
    ) == 0
    assert "1 個のインストールモジュール" in capsys.readouterr().out
    assert validate_install_manifests_module.main(
        [
            "--repo-root",
            str(repo_root),
            "--modules-manifest-path",
            str(manifests / "install-modules.json"),
            "--profiles-manifest-path",
            str(manifests / "install-profiles.json"),
        ]
    ) == 0


def test_validate_install_manifests_reports_missing_full_and_component_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    (repo_root / "scripts").mkdir()
    (repo_root / "scripts" / "a.js").write_text("a\n", encoding="utf-8")
    manifests = repo_root / "manifests"
    write_json(
        manifests / "install-modules.json",
        {
            "version": 1,
            "modules": [
                {"id": "mod-a"},
                {"id": "mod-b", "paths": ["scripts/a.js"]},
            ],
        },
    )
    write_json(
        manifests / "install-profiles.json",
        {
            "version": 1,
            "profiles": {
                "core": {"modules": ["mod-a"]},
                "developer": {"modules": ["mod-a", None]},
                "security": {"modules": ["missing"]},
                "research": {"modules": ["mod-a"]},
            },
        },
    )
    write_json(
        manifests / "install-components.json",
        {
            "version": 1,
            "components": [
                {"id": "oops", "family": "baseline", "modules": ["mod-a"]},
                {"id": "lang:js", "family": "language", "modules": ["mod-a", "mod-a", None]},
            ],
        },
    )

    assert (
        validate_install_manifests(
            repo_root=repo_root,
            modules_manifest_path=manifests / "install-modules.json",
            profiles_manifest_path=manifests / "install-profiles.json",
            components_manifest_path=manifests / "install-components.json",
        )
        == 1
    )
    stderr = capsys.readouterr().err
    assert "必須のインストールプロファイルがありません: full" in stderr
    assert "プロファイル developer は不明なモジュール None を参照しています" in stderr
    assert "プロファイル security は不明なモジュール missing を参照しています" in stderr
    assert "コンポーネント oops は想定される baseline のプレフィックス baseline: と一致しません" in stderr
    assert "コンポーネント lang:js に重複したモジュール mod-a が含まれています" in stderr


def test_validate_install_manifests_covers_object_and_entrypoint_branches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    manifests = repo_root / "manifests"
    modules_path = manifests / "install-modules.json"
    profiles_path = manifests / "install-profiles.json"
    components_path = manifests / "install-components.json"

    write_json(modules_path, {"version": 1, "modules": {}})
    write_json(profiles_path, {"version": 1, "profiles": {}})

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=modules_path,
        profiles_manifest_path=profiles_path,
    ) == 1
    assert "install-modules.json modules は配列である必要があります" in capsys.readouterr().err

    write_json(modules_path, {"version": 1, "modules": [{"id": "mod-a"}]})
    write_json(profiles_path, {"version": 1, "profiles": []})

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=modules_path,
        profiles_manifest_path=profiles_path,
    ) == 1
    assert "install-profiles.json の profiles はオブジェクトである必要があります" in capsys.readouterr().err

    write_json(
        profiles_path,
        {
            "version": 1,
            "profiles": {
                "core": {"modules": []},
                "developer": {"modules": []},
                "security": {"modules": []},
                "research": {"modules": []},
                "full": {"modules": []},
            },
        },
    )
    components_path.write_text("[]", encoding="utf-8")

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=modules_path,
        profiles_manifest_path=profiles_path,
        components_manifest_path=components_path,
    ) == 1
    assert "install-components.json はオブジェクトである必要があります" in capsys.readouterr().err

    write_json(
        components_path,
        {
            "version": 1,
            "components": [
                {"id": "", "family": "baseline", "modules": ["mod-a"]},
                {"id": "language:missing", "family": "language", "modules": ["missing"]},
            ],
        },
    )

    assert validate_install_manifests(
        repo_root=repo_root,
        modules_manifest_path=modules_path,
        profiles_manifest_path=profiles_path,
        components_manifest_path=components_path,
    ) == 1
    stderr = capsys.readouterr().err
    assert "コンポーネントエントリの id が不足しているか無効です" in stderr
    assert "コンポーネント language:missing は不明なモジュール missing を参照しています" in stderr

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "validate_install_manifests.py",
            "--repo-root",
            str(repo_root),
            "--modules-manifest-path",
            str(modules_path),
            "--profiles-manifest-path",
            str(profiles_path),
            "--components-manifest-path",
            str(components_path),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.ci.validate_install_manifests", run_name="__main__")

    assert excinfo.value.code == 1
