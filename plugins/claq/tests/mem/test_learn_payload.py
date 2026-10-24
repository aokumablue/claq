"""claq.mem.learn_payload のテスト。

デシジョンテーブル:
  - 全フィールドあり     → 全キーを含む JSON
  - 一部が空文字列       → そのキーを落とす（learn 側の既定値を活かす）
  - すべて未設定         → 空の JSON オブジェクト
  - source/status を設定 → 無視する（docs/adr/untrusted-input-prompt-boundary.md: 呼び出し元に権限が無い）
"""

from __future__ import annotations

import json
import runpy
import sys

import pytest

from claq.mem.learn_payload import build_payload, main


@pytest.mark.parametrize(
    ("environ", "expected"),
    [
        (
            {
                "CLAQ_LEARN_KEY": "k",
                "CLAQ_LEARN_KIND": "pitfall",
                "CLAQ_LEARN_SCOPE": "repo",
                "CLAQ_LEARN_TITLE": "t",
                "CLAQ_LEARN_BODY": "b",
                "CLAQ_LEARN_DOMAIN": "testing",
                "CLAQ_LEARN_CONFIDENCE": "0.8",
                "CLAQ_LEARN_SOURCE_REF": "docs/x.md",
            },
            {
                "key": "k",
                "kind": "pitfall",
                "scope": "repo",
                "title": "t",
                "body": "b",
                "domain": "testing",
                "confidence": "0.8",
                "source_ref": "docs/x.md",
            },
        ),
        (
            {
                "CLAQ_LEARN_KIND": "fact",
                "CLAQ_LEARN_TITLE": "t",
                "CLAQ_LEARN_BODY": "",
                "CLAQ_LEARN_SOURCE": "human",
                "CLAQ_LEARN_STATUS": "active",
            },
            {"kind": "fact", "title": "t"},
        ),
        ({}, {}),
    ],
    ids=["all-fields", "empty-and-source-status-dropped", "no-env"],
)
def test_build_payload(environ: dict[str, str], expected: dict[str, str]) -> None:
    """``CLAQ_LEARN_*`` の空でない対象フィールドだけを payload へ載せる。"""
    assert build_payload(environ) == expected


def test_main_writes_json_to_stdout(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """main が payload を JSON として stdout へ書き、0 を返すこと。"""
    monkeypatch.setenv("CLAQ_LEARN_KIND", "howto")
    monkeypatch.setenv("CLAQ_LEARN_TITLE", "非 ASCII も素通しする ✓")

    assert main() == 0
    assert json.loads(capsys.readouterr().out) == {"kind": "howto", "title": "非 ASCII も素通しする ✓"}


def test_module_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """``-m`` 実行（launcher の runpy 経路）が SystemExit(0) で終わること。"""
    monkeypatch.setattr(sys, "argv", ["claq.mem.learn_payload"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("claq.mem.learn_payload", run_name="__main__")

    assert excinfo.value.code == 0
