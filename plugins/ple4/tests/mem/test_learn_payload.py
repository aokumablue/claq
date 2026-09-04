"""ple4.mem.learn_payload のテスト。

デシジョンテーブル:
  - 全フィールドあり     → 全キーを含む JSON
  - 一部が空文字列       → そのキーを落とす（learn 側の既定値を活かす）
  - すべて未設定         → 空の JSON オブジェクト
  - source/status を設定 → 無視する（ADR-0007: 呼び出し元に権限が無い）
"""

from __future__ import annotations

import json
import runpy
import sys

import pytest

from ple4.mem.learn_payload import build_payload, main


def test_build_payload_collects_all_fields() -> None:
    """``PLE4_LEARN_*`` の全フィールドが payload へ載ること。"""
    environ = {
        "PLE4_LEARN_KEY": "k",
        "PLE4_LEARN_KIND": "pitfall",
        "PLE4_LEARN_SCOPE": "repo",
        "PLE4_LEARN_TITLE": "t",
        "PLE4_LEARN_BODY": "b",
        "PLE4_LEARN_DOMAIN": "testing",
        "PLE4_LEARN_CONFIDENCE": "0.8",
        "PLE4_LEARN_SOURCE_REF": "docs/x.md",
    }

    assert build_payload(environ) == {
        "key": "k",
        "kind": "pitfall",
        "scope": "repo",
        "title": "t",
        "body": "b",
        "domain": "testing",
        "confidence": "0.8",
        "source_ref": "docs/x.md",
    }


def test_build_payload_drops_empty_and_unknown_fields() -> None:
    """空文字列は落とし、source/status のような対象外キーは拾わないこと。"""
    environ = {
        "PLE4_LEARN_KIND": "fact",
        "PLE4_LEARN_TITLE": "t",
        "PLE4_LEARN_BODY": "",
        "PLE4_LEARN_SOURCE": "human",
        "PLE4_LEARN_STATUS": "active",
    }

    assert build_payload(environ) == {"kind": "fact", "title": "t"}


def test_build_payload_returns_empty_without_env() -> None:
    """環境変数が 1 つも無ければ空の payload になること。"""
    assert build_payload({}) == {}


def test_main_writes_json_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """main が payload を JSON として stdout へ書き、0 を返すこと。"""
    monkeypatch.setenv("PLE4_LEARN_KIND", "howto")
    monkeypatch.setenv("PLE4_LEARN_TITLE", "非 ASCII も素通しする ✓")

    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {
        "kind": "howto",
        "title": "非 ASCII も素通しする ✓",
    }


def test_module_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``-m`` 実行（launcher の runpy 経路）が SystemExit(0) で終わること。"""
    monkeypatch.setattr(sys, "argv", ["ple4.mem.learn_payload"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("ple4.mem.learn_payload", run_name="__main__")

    assert excinfo.value.code == 0
