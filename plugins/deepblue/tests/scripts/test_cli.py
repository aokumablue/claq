"""CLI コマンドとカタログ機能のテスト。

カタログプロファイル読み取り、スキルヘルスダッシュボード、
スキル作成出力の整形を対象とする。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from pathlib import Path

import devgear.install_catalog as catalog
import devgear.skill_create_output as skill_create_output


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

    assert "Discover devgear install components and profiles" in stdout.getvalue()


def test_skill_create_output_renders_header() -> None:
    stdout = io.StringIO()
    with redirect_stdout(stdout):
        assert skill_create_output.main(["header", "devgear"]) == 0

    rendered = stdout.getvalue()
    assert "devgear Skill Creator" in rendered
    assert "devgear" in rendered


def test_skill_create_output_renders_gitlab_footer() -> None:
    rendered = skill_create_output.render_footer("gitlab")

    assert "GitLab CLI: glab mr view" in rendered


def test_skill_create_output_renders_analysis_phase() -> None:
    stdout = io.StringIO()
    original_stdin = sys.stdin
    sys.stdin = io.StringIO("")
    with redirect_stdout(stdout):
        try:
            assert skill_create_output.main(["analyze-phase"]) == 0
        finally:
            sys.stdin = original_stdin

    assert "[RUN] Analyzing Repository..." in skill_create_output.strip_ansi(stdout.getvalue())
