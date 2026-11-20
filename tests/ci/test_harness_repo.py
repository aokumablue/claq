"""開発用の repo モード監査（``claq_dev.harness_repo``）のテスト。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from claq_dev import harness_repo

from ._support import _exit_code_as_main, _write_repo_markers
from ._support import _isolate_home as _isolate_home


def test_build_report_scores_repo_markers(tmp_path: Path) -> None:
    """プラグイン提供元の最小構造だけの root を repo モードで採点する。

    surface が 1 つも無い fixture では予算系 2 件（context-always-on-budget /
    cost-no-oversized-surface）が自明に通る。surface の欠落は tool-agent-count / tool-skill-count が見る。
    """
    root = tmp_path / "plugins" / "claq"
    _write_repo_markers(root)
    (root / "commands").mkdir()

    report = harness_repo.build_report("repo", root)

    assert report["target_mode"] == "repo"
    assert (report["overall_score"], report["max_score"], len(report["checks"])) == (5, 58, 22)
    assert report["categories"]["Tool Coverage"]["max_points"] == 10
    assert report["top_actions"][0]["path"] == "hooks/hooks.json"


def test_main_prints_repo_report_as_json(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """``--format json`` は repo モードのレポートを出し、不合格があれば 1 を返す。"""
    root = tmp_path / "plugins" / "claq"
    _write_repo_markers(root)

    assert harness_repo.main(["--format", "json", "--root", str(root)]) == 1
    assert json.loads(capsys.readouterr().out)["target_mode"] == "repo"


def test_main_entrypoint_exits_with_audit_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """`python -m` 起動は main の戻り値で終了し、不合格のある repo では 1 になる。"""
    root = tmp_path / "plugins" / "claq"
    _write_repo_markers(root)
    monkeypatch.setattr(sys, "argv", ["harness_repo.py", "--scope", "repo", "--format=json", "--root", str(root)])

    assert _exit_code_as_main("claq_dev.harness_repo") == 1
