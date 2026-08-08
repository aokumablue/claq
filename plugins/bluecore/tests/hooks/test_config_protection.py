"""config_protection フックの apply_patch 生文字列入力テスト。

test_hook_helpers.py の既存テストは tool_input が {"input": patch} 形式（dict）を対象とする。
本モジュールは tool_input が生のパッチ文字列（dict ではなく str）として渡る
Copilot CLI 実行パスをカバーする。
"""

from __future__ import annotations

import json

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


def test_main_blocks_protected_file_for_apply_patch_string(monkeypatch) -> None:
    """tool_input が生文字列の apply_patch で保護ファイルをブロックする。"""
    messages: list[str] = []
    monkeypatch.setattr(config_protection, "read_raw_stdin", lambda: _apply_patch_payload("/tmp/.prettierrc"))
    monkeypatch.setattr(config_protection, "write_stderr", messages.append)

    assert config_protection.main() == 2
    assert "BLOCKED: Modifying .prettierrc is not allowed." in "".join(messages)


def test_main_allows_non_protected_file_for_apply_patch_string(monkeypatch) -> None:
    """tool_input が生文字列の apply_patch で非保護ファイルを通過させる。"""
    messages: list[str] = []
    monkeypatch.setattr(config_protection, "read_raw_stdin", lambda: _apply_patch_payload("/tmp/example.txt"))
    monkeypatch.setattr(config_protection, "write_stderr", messages.append)

    assert config_protection.main() == 0
    assert messages == []


def test_main_blocks_protected_file_for_copilot_lowercase_write(monkeypatch) -> None:
    """Copilot CLI の lowercase tool_name（write）でも保護ファイルをブロックする。

    extract_file_paths() は apply_patch 以外は tool_name の値によらず
    file_path フィールドを抽出するため、正規化なしでも動作することの回帰確認。
    """
    messages: list[str] = []
    payload = json.dumps({"tool_name": "write", "tool_input": {"file_path": ".prettierrc"}})
    monkeypatch.setattr(config_protection, "read_raw_stdin", lambda: payload)
    monkeypatch.setattr(config_protection, "write_stderr", messages.append)

    assert config_protection.main() == 2
    assert "BLOCKED: Modifying .prettierrc is not allowed." in "".join(messages)
