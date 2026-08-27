"""フックヘルパー関数と挙動のテスト。

ドキュメントファイル警告、設定保護、セッションライフサイクル、
およびコンパクト提案ロジックを対象とする。
"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout

import pytest

from bluecore.hooks import (
    config_protection as config_protection,
)
from bluecore.hooks.hook_common import is_truthy


def _patch_stdin_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    """hook_common.read_raw_stdin* が使う select を常に ready 扱いにする。

    io.StringIO は実 fd を持たないため、select.select をそのまま通すと
    io.UnsupportedOperation で落ちる（_stdin_ready の TTY/タイムアウト
    ガードは launcher._read_stdin から移設済み）。
    """
    from bluecore.hooks import hook_common

    monkeypatch.setattr(hook_common.select, "select", lambda r, w, x, t: (r, [], []))


def _run_config_protection(monkeypatch: pytest.MonkeyPatch, payload: dict) -> tuple[int, str, str]:
    """stdin を差し替えて config_protection.main を実行する。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        payload: フック stdin に渡す dict。

    Returns:
        (終了コード, stdout, stderr) のタプル。
    """
    _patch_stdin_ready(monkeypatch)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stderr = io.StringIO()
    stdout = io.StringIO()
    with redirect_stderr(stderr), redirect_stdout(stdout):
        code = config_protection.main()
    return code, stdout.getvalue(), stderr.getvalue()


def test_config_protection_blocks_protected_file(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": "eslint.config.js"}},
    )
    assert code == 2
    assert "Modifying eslint.config.js is not allowed" in stderr
    deny = json.loads(stdout)
    assert deny["permissionDecision"] == "deny"


def test_config_protection_allows_model_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """埋め込みモデル廃止に伴い model.json は保護対象から外れている。"""
    code, _stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": "plugins/bluecore/model.json"}},
    )
    assert code == 0
    assert stderr == ""


def test_config_protection_allows_safe_file(monkeypatch: pytest.MonkeyPatch) -> None:
    code, stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file_path": "README.md"}},
    )
    # 許可時は stdout は空（パススルー不要）
    assert code == 0
    assert stdout == ""
    assert stderr == ""


def test_config_protection_blocks_protected_file_in_apply_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex の apply_patch パッチ内の保護ファイルをブロックする。"""
    patch = "*** Begin Patch\n*** Update File: ruff.toml\n@@\n-a\n+b\n*** End Patch"
    code, _stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "apply_patch", "tool_input": {"input": patch}},
    )
    assert code == 2
    assert "Modifying ruff.toml is not allowed" in stderr


def test_config_protection_blocks_unparseable_apply_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """パース不能な apply_patch 入力は fail-closed でブロックする。"""
    code, _stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "apply_patch", "tool_input": {"input": "garbage"}},
    )
    assert code == 2
    assert "Could not determine target files" in stderr


def test_config_protection_allows_safe_apply_patch(monkeypatch: pytest.MonkeyPatch) -> None:
    """保護対象を含まない apply_patch は許可する。"""
    patch = "*** Begin Patch\n*** Update File: src/main.py\n@@\n-a\n+b\n*** End Patch"
    code, _stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "apply_patch", "tool_input": {"input": patch}},
    )
    assert code == 0
    assert stderr == ""


def test_config_protection_blocks_legacy_file_field(monkeypatch: pytest.MonkeyPatch) -> None:
    """file フィールドのみ持つ入力でも保護ファイルをブロックする。"""
    code, _stdout, stderr = _run_config_protection(
        monkeypatch,
        {"tool_name": "Write", "tool_input": {"file": "biome.json"}},
    )
    assert code == 2
    assert "Modifying biome.json is not allowed" in stderr


def test_config_protection_entrypoint_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_stdin_ready(monkeypatch)
    payload = json.dumps({"tool_input": {"file_path": "README.md"}})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    monkeypatch.setattr(sys, "argv", ["config_protection.py"])

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.hooks.config_protection", run_name="__main__")

    assert excinfo.value.code == 0


def test_hook_common_is_truthy_handles_falsey_values() -> None:
    assert is_truthy(None) is False
    assert is_truthy("") is False
    assert is_truthy("0") is False
    assert is_truthy(" no ") is False

