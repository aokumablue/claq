"""config_protection フックの apply_patch 生文字列入力テスト。

test_hook_helpers.py の既存テストは tool_input が {"input": patch} 形式（dict）を対象とする。
本モジュールは tool_input が生のパッチ文字列（dict ではなく str）として渡る
Copilot CLI 実行パスをカバーする。

config_protection は emit_block_output（hook_common から自モジュール名前空間へ
直接 import）でブロック出力を書くため、write_stderr は自モジュールに存在しない。
capsys で実際の stdout/stderr を検証する（block_no_verify のテストと同じ方式）。
"""

from __future__ import annotations

import json

import pytest

from bluecore.hooks import config_protection


def _apply_patch_payload(path: str) -> str:
    """指定パスへの apply_patch ペイロード JSON を生成する。

    Args:
        path: パッチ対象ファイルパス。

    Returns:
        tool_input が生文字列のパッチペイロード JSON 文字列。

    Raises:
        例外は発生しません。
    """
    patch_text = (
        "*** Begin Patch\n"
        f"*** Update File: {path}\n"
        "@@\n"
        "-old\n"
        "+new\n"
        "*** End Patch\n"
    )
    return json.dumps({"tool_name": "apply_patch", "tool_input": patch_text})


def test_main_blocks_protected_file_for_apply_patch_string(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tool_input が生文字列の apply_patch で保護ファイルをブロックする。"""
    monkeypatch.setattr(
        config_protection,
        "read_raw_stdin_with_truncation",
        lambda: (_apply_patch_payload("/tmp/.prettierrc"), False),
    )

    assert config_protection.main() == 2
    assert "BLOCKED: Modifying .prettierrc is not allowed." in capsys.readouterr().err


def test_main_allows_non_protected_file_for_apply_patch_string(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """tool_input が生文字列の apply_patch で非保護ファイルを通過させる。"""
    monkeypatch.setattr(
        config_protection,
        "read_raw_stdin_with_truncation",
        lambda: (_apply_patch_payload("/tmp/example.txt"), False),
    )

    assert config_protection.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_blocks_protected_file_for_copilot_lowercase_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Copilot CLI の lowercase tool_name（write）でも保護ファイルをブロックする。

    extract_file_paths() は apply_patch 以外は tool_name の値によらず
    file_path フィールドを抽出するため、正規化なしでも動作することの回帰確認。
    """
    payload = json.dumps({"tool_name": "write", "tool_input": {"file_path": ".prettierrc"}})
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))

    assert config_protection.main() == 2
    assert "BLOCKED: Modifying .prettierrc is not allowed." in capsys.readouterr().err


def test_main_skips_non_write_tool_even_with_protected_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """matcher が "*" に広がっても、Read 等の非書込みツールでは保護判定自体を行わない（早期 return）。"""
    payload = json.dumps({"tool_name": "Read", "tool_input": {"file_path": ".prettierrc"}})
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))

    assert config_protection.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_skips_copilot_lowercase_non_write_tool(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Copilot CLI の lowercase 非書込みツール名（read）でも早期 return する。"""
    payload = json.dumps({"tool_name": "read", "tool_input": {"file_path": ".prettierrc"}})
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))

    assert config_protection.main() == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_main_blocks_protected_file_for_copilot_lowercase_edit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Copilot CLI の lowercase tool_name（edit）でも保護ファイルをブロックする。"""
    payload = json.dumps({"tool_name": "edit", "tool_input": {"file_path": "biome.json"}})
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))

    assert config_protection.main() == 2
    assert "BLOCKED: Modifying biome.json is not allowed." in capsys.readouterr().err


def test_main_blocks_on_truncated_input(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """入力が切り捨てられた場合は fail-closed でブロックする（自己完結 guard）。"""
    monkeypatch.setattr(
        config_protection, "read_raw_stdin_with_truncation", lambda: ("{not-even-json", True)
    )

    assert config_protection.main() == 2
    assert "BLOCKED: Hook input exceeded" in capsys.readouterr().err
