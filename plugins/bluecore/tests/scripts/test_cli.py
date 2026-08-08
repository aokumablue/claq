"""CLI コマンドとカタログ機能のテスト。

カタログプロファイル読み取り、スキルヘルスダッシュボードを対象とする。
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import bluecore.install_catalog as catalog


def test_catalog_profiles_reads_temp_manifests(tmp_path: Path) -> None:
    manifests_dir = tmp_path / "manifests"
    manifests_dir.mkdir()
    (manifests_dir / "install-modules.json").write_text(
        json.dumps({"version": "1", "modules": [{"id": "core", "targets": ["claude"]}]}),
        encoding="utf-8",
    )
    (manifests_dir / "install-profiles.json").write_text(
        json.dumps({"version": "1", "profiles": {"default": {"description": "Default", "modules": ["core"]}}}),
        encoding="utf-8",
    )

    payload = catalog.list_install_profiles({"repoRoot": tmp_path})
    assert payload[0]["id"] == "default"
    assert payload[0]["moduleCount"] == 1


def test_catalog_help_prints_usage() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert catalog.main(["--help"]) == 0

    assert "Discover bluecore install components and profiles" in stdout.getvalue()
