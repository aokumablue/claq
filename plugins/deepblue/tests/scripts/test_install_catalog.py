"""install_catalog モジュールのテスト。"""

from __future__ import annotations

import json
import runpy
from pathlib import Path
from unittest.mock import patch

import pytest

import devgear.install_catalog as catalog


def _write_manifests(tmp_path: Path, include_components: bool = True) -> dict[str, object]:
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir(parents=True, exist_ok=True)
    (manifests_dir / "install-modules.json").write_text(
        json.dumps(
            {
                "version": "1",
                "modules": [
                    {
                        "id": "mod-a",
                        "kind": "module",
                        "description": "Module A",
                        "targets": ["claude"],
                        "defaultInstall": True,
                        "cost": 1,
                        "stability": "stable",
                        "dependencies": [],
                    },
                    {
                        "id": "mod-b",
                        "kind": "module",
                        "description": "Module B",
                        "targets": ["claude"],
                        "defaultInstall": False,
                        "cost": 2,
                        "stability": "beta",
                        "dependencies": ["mod-a"],
                    },
                    {
                        "id": "mod-c",
                        "kind": "module",
                        "description": "Module C",
                        "targets": ["docker"],
                        "defaultInstall": False,
                        "cost": 3,
                        "stability": "stable",
                        "dependencies": [],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    (manifests_dir / "install-profiles.json").write_text(
        json.dumps(
            {
                "version": "1",
                "profiles": {
                    "default": {"description": "Default", "modules": ["mod-a", "mod-b", "mod-b"]},
                    "security": {"description": "Security", "modules": ["mod-b"]},
                },
            }
        ),
        encoding="utf-8",
    )
    if include_components:
        (manifests_dir / "install-components.json").write_text(
            json.dumps(
                {
                    "version": "1",
                    "components": [
                        {
                            "id": "language:foo",
                            "family": "language",
                            "description": "Foo",
                            "modules": ["mod-a", "mod-missing"],
                        },
                        {"id": "baseline:bar", "family": "baseline", "description": "Bar", "modules": ["mod-a"]},
                        {"id": "agent:skip", "family": "agent", "description": "Skip", "modules": ["mod-b"]},
                        {
                            "id": "language:docker-only",
                            "family": "language",
                            "description": "Docker only",
                            "modules": ["mod-c"],
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )
    return {"repoRoot": tmp_path}


def _load_sample_manifests(tmp_path: Path) -> dict[str, object]:
    options = _write_manifests(tmp_path)
    return catalog.load_install_manifests(options)


def test_normalize_family_and_dedupe_strings() -> None:
    assert catalog.normalize_family(None) is None
    assert catalog.normalize_family(" Lang ") == "language"
    assert catalog.normalize_family("skill") == "skill"
    assert catalog.dedupe_strings([" a ", "", "a", "b", "b"]) == ["a", "b"]
    assert catalog.dedupe_strings("not-a-list") == []


def test_get_manifest_paths_and_read_json(tmp_path: Path) -> None:
    paths = catalog.get_manifest_paths(tmp_path)
    assert paths["modulesPath"] == tmp_path / "manifests" / "install-modules.json"
    assert paths["profilesPath"] == tmp_path / "manifests" / "install-profiles.json"
    assert paths["componentsPath"] == tmp_path / "manifests" / "install-components.json"

    payload = tmp_path / "payload.json"
    payload.write_text("{\"ok\": true}", encoding="utf-8")
    assert catalog.read_json(payload, "payload") == {"ok": True}

    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Failed to read broken"):
        catalog.read_json(broken, "broken")


def test_load_install_manifests_roundtrip_and_missing_files(tmp_path: Path) -> None:
    manifests = _load_sample_manifests(tmp_path)
    assert manifests["modulesVersion"] == "1"
    assert manifests["profilesVersion"] == "1"
    assert manifests["componentsVersion"] == "1"
    assert list(manifests["modulesById"]) == ["mod-a", "mod-b", "mod-c"]
    assert list(manifests["componentsById"]) == ["language:foo", "baseline:bar", "agent:skip", "language:docker-only"]

    empty_root = tmp_path / "missing"
    with pytest.raises(RuntimeError, match="Install manifests not found"):
        catalog.load_install_manifests({"repoRoot": empty_root})

    no_components_root = tmp_path / "no-components"
    _write_manifests(no_components_root, include_components=False)
    no_components = catalog.load_install_manifests({"repoRoot": no_components_root})
    assert no_components["components"] == []
    assert no_components["componentsVersion"] is None


def test_intersect_targets_handles_empty_and_matching_modules() -> None:
    assert catalog._intersect_targets([]) == []
    assert catalog._intersect_targets([{"targets": ["claude"]}, {"targets": ["claude"]}]) == ["claude"]
    assert catalog._intersect_targets([{"targets": ["claude"]}, {"targets": ["docker"]}]) == []


def test_list_install_profiles_and_components(tmp_path: Path) -> None:
    manifests = _load_sample_manifests(tmp_path)

    profiles = catalog.list_install_profiles({"repoRoot": tmp_path})
    assert profiles == [
        {"id": "default", "description": "Default", "moduleCount": 3},
        {"id": "security", "description": "Security", "moduleCount": 1},
    ]

    with patch.object(catalog, "load_install_manifests", return_value=manifests):
        assert catalog.list_install_components({"family": "language", "target": "claude"}) == [
            {
                "id": "language:foo",
                "family": "language",
                "description": "Foo",
                "moduleIds": ["mod-a", "mod-missing"],
                "moduleCount": 2,
                "targets": ["claude"],
            }
        ]

    assert {component["id"] for component in catalog.list_install_components({"repoRoot": tmp_path})} == {
        "language:foo",
        "baseline:bar",
        "agent:skip",
    }

    with patch.object(
        catalog,
        "load_install_manifests",
        return_value={
            **manifests,
            "components": [manifests["components"][0], "bad"],
        },
    ):
        assert catalog.list_install_components({"repoRoot": tmp_path})[0]["id"] == "language:foo"


def test_list_install_components_rejects_unknown_family_and_target(tmp_path: Path) -> None:
    manifests = _load_sample_manifests(tmp_path)
    with patch.object(catalog, "load_install_manifests", return_value=manifests):
        with pytest.raises(ValueError, match="Unknown component family"):
            catalog.list_install_components({"family": "unknown"})
        with pytest.raises(ValueError, match="Unknown install target"):
            catalog.list_install_components({"target": "docker"})


def test_get_install_component_validates_and_resolves_modules(tmp_path: Path) -> None:
    manifests = _load_sample_manifests(tmp_path)
    with patch.object(catalog, "load_install_manifests", return_value=manifests):
        with pytest.raises(ValueError, match="An install component ID is required"):
            catalog.get_install_component("", {})

        with pytest.raises(ValueError, match="Unknown install component"):
            catalog.get_install_component("missing", {})

        component = catalog.get_install_component("language:foo", {})

    assert component["moduleIds"] == ["mod-a", "mod-missing"]
    assert component["targets"] == ["claude"]
    assert [module["id"] for module in component["modules"]] == ["mod-a"]


def test_normalize_options_and_main_branches(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    manifests = _load_sample_manifests(tmp_path)
    sample_profiles = [{"id": "default", "description": "Default", "moduleCount": 2}]
    sample_components = [
        {
            "id": "language:foo",
            "family": "language",
            "description": "Foo",
            "moduleIds": ["mod-a"],
            "moduleCount": 1,
            "targets": ["claude"],
        }
    ]
    sample_component = {
        "id": "language:foo",
        "family": "language",
        "description": "Foo",
        "moduleIds": ["mod-a"],
        "moduleCount": 1,
        "targets": ["claude"],
        "modules": [
            {
                "id": "mod-a",
                "kind": "module",
                "description": "Module A",
                "targets": ["claude"],
                "defaultInstall": True,
                "cost": 1,
                "stability": "stable",
            }
        ],
    }

    assert catalog._normalize_options([])["help"] is True
    assert catalog._normalize_options(["show", "language:foo", "--family", "Lang", "--json"]) == {
        "command": "show",
        "componentId": "language:foo",
        "family": "language",
        "target": None,
        "json": True,
        "help": False,
    }
    assert catalog._normalize_options(["components", "--help"]) == {
        "command": "components",
        "componentId": None,
        "family": None,
        "target": None,
        "json": False,
        "help": True,
    }
    assert catalog._normalize_options(["components", "--target", "claude"]) == {
        "command": "components",
        "componentId": None,
        "family": None,
        "target": "claude",
        "json": False,
        "help": False,
    }
    with pytest.raises(ValueError, match="Missing value for --family"):
        catalog._normalize_options(["components", "--family"])
    with pytest.raises(ValueError, match="Missing value for --target"):
        catalog._normalize_options(["components", "--target"])
    with pytest.raises(ValueError, match="Unknown argument: --bad"):
        catalog._normalize_options(["components", "--bad"])

    with patch.object(catalog, "load_install_manifests", return_value=manifests), patch.object(
        catalog, "list_install_profiles", return_value=sample_profiles
    ), patch.object(catalog, "list_install_components", return_value=sample_components), patch.object(
        catalog, "get_install_component", return_value=sample_component
    ):
        assert catalog.main(["--help"]) == 0
        assert "Discover devgear install components and profiles" in capsys.readouterr().out

        assert catalog.main(["profiles"]) == 0
        assert "Install profiles:" in capsys.readouterr().out

        assert catalog.main(["profiles", "--json"]) == 0
        assert '"profiles"' in capsys.readouterr().out

        assert catalog.main(["components"]) == 0
        assert "Install components:" in capsys.readouterr().out

        assert catalog.main(["components", "--json"]) == 0
        assert '"components"' in capsys.readouterr().out

        assert catalog.main(["show", "language:foo"]) == 0
        assert "Install component: language:foo" in capsys.readouterr().out

        assert catalog.main(["show", "language:foo", "--json"]) == 0
        assert '"id": "language:foo"' in capsys.readouterr().out

    assert catalog.main(["unknown"]) == 1
    assert "Unknown catalog command: unknown" in capsys.readouterr().err
    assert catalog.main(["show"]) == 1
    assert "Catalog show requires an install component ID" in capsys.readouterr().err


def test_install_catalog_main_block_invokes_main(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(catalog.sys, "argv", ["install_catalog.py", "--help"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.install_catalog", run_name="__main__")

    assert excinfo.value.code == 0
