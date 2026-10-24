"""tests/ci/test_harness_audit*.py が共有する補助（ファイル書き出し・repo マーカー・HOME 隔離）。"""

from __future__ import annotations

from pathlib import Path

import pytest


def _write_files(root: Path, files: dict[str, str]) -> None:
    """root 相対パス → 本文の対応どおりにファイルを書き出す。"""
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _write_repo_markers(root: Path, *, include_harness: bool = True) -> None:
    """repo モード判定に必要なマーカー（plugin.json・agents・skills・harness_audit.py）を作る。"""
    _write_files(root, {".claude-plugin/plugin.json": "{}"})
    (root / "agents").mkdir(exist_ok=True)
    (root / "skills").mkdir(exist_ok=True)
    if include_harness:
        _write_files(root, {"src/claq/ci/harness_audit.py": ""})


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`find_plugin_install` が実ホームの導入状態を読まないよう、ホームを tmp 配下へ固定する。"""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CLAQ_HOME", raising=False)
