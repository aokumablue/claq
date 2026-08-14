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


def _spy_block(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """emit_block_output を spy し、理由文字列のリストを返す。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。

    Returns:
        emit_block_output に渡された reason の蓄積リスト。
    """
    reasons: list[str] = []

    def _spy(reason: str) -> int:
        """ブロック理由を記録し、Claude 相当の終了コード 2 を返す。"""
        reasons.append(reason)
        return 2

    monkeypatch.setattr(config_protection, "emit_block_output", _spy)
    return reasons


def _run_protection(monkeypatch: pytest.MonkeyPatch, payload: dict | str, *, truncated: bool = False) -> int:
    """stdin を差し替えて config_protection.main を実行する。

    Args:
        monkeypatch: pytest の monkeypatch フィクスチャ。
        payload: JSON にできる dict、または生文字列。
        truncated: 切り捨てフラグ。

    Returns:
        main() の終了コード。
    """
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(
        config_protection,
        "read_raw_stdin_with_truncation",
        lambda: (raw, truncated),
    )
    return config_protection.main()


def test_config_protection_blocks_protected_file_from_native_camel_case_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R-03: native toolName=edit / toolArgs.file_path=ruff.toml を deny する。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(
        monkeypatch,
        {"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}},
    )
    assert rc == 2
    assert len(reasons) == 1
    assert "ruff.toml" in reasons[0]


def test_config_protection_blocks_legacy_file_from_native_tool_args(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native toolArgs の旧 file キーでも保護ファイルを deny する。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(
        monkeypatch,
        {"toolName": "edit", "toolArgs": {"file": "ruff.toml"}},
    )
    assert rc == 2
    assert len(reasons) == 1
    assert "ruff.toml" in reasons[0]


def test_config_protection_denies_conflicting_payload_when_any_target_is_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """混在 payload は、いずれかのコンテナが保護対象なら deny する。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(
        monkeypatch,
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "src/sample.py"},
            "toolName": "edit",
            "toolArgs": {"file_path": "ruff.toml"},
        },
    )
    assert rc == 2
    assert len(reasons) == 1
    assert "ruff.toml" in reasons[0]

    reasons.clear()
    rc = _run_protection(
        monkeypatch,
        {
            "tool_name": "Edit",
            "tool_input": {"file_path": "ruff.toml"},
            "toolName": "edit",
            "toolArgs": {"file_path": "src/sample.py"},
        },
    )
    assert rc == 2
    assert len(reasons) == 1
    assert "ruff.toml" in reasons[0]


def test_config_protection_allows_unprotected_native_camel_case_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """native camelCase の非保護パスは allow（block 未呼び出し）。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(
        monkeypatch,
        {"toolName": "multiedit", "toolArgs": {"file_path": "src/sample.py"}},
    )
    assert rc == 0
    assert reasons == []


@pytest.mark.parametrize(
    ("payload", "expect_block", "reason_snippet"),
    [
        (
            {"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}},
            True,
            "ruff.toml",
        ),
        (
            {"toolName": "edit", "toolArgs": json.dumps({"file_path": "ruff.toml"})},
            True,
            "ruff.toml",
        ),
        (
            {"tool_name": "Write", "tool_args": {"file_path": ".eslintrc"}},
            True,
            ".eslintrc",
        ),
        (
            {"toolName": "edit", "toolArgs": {"file_path": "src/ruff.toml"}},
            True,
            "ruff.toml",
        ),
        (
            {"toolName": "bash", "toolArgs": {"file_path": "ruff.toml"}},
            False,
            None,
        ),
        (
            {"toolName": "edit", "toolArgs": {}},
            False,
            None,
        ),
        (
            {"toolName": "apply_patch", "toolArgs": {"input": "garbage"}},
            True,
            "Could not determine target files",
        ),
        (
            {
                "tool_name": "Edit",
                "tool_input": {"file_path": "src/a.py"},
                "toolArgs": {"file_path": "src/b.py"},
            },
            False,
            None,
        ),
        (
            {
                "tool_name": "Edit",
                "tool_input": None,
                "toolArgs": {"file_path": "ruff.toml"},
            },
            True,
            "ruff.toml",
        ),
    ],
    ids=[
        "dt03-1-snake-ruff",
        "dt03-3-toolargs-json-string",
        "dt03-4-tool_args-eslintrc",
        "dt03-7-basename",
        "dt03-8-non-write-bash",
        "dt03-9-no-path-key",
        "dt03-10-apply-patch-fail-closed",
        "dt03-13-both-unprotected",
        "dt03-14-tool-input-none-still-scans",
    ],
)
def test_config_protection_remaining_dt03_rows(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict,
    expect_block: bool,
    reason_snippet: str | None,
) -> None:
    """DT-03 残行: JSON 文字列・tool_args・basename・非書込み・fail-closed。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(monkeypatch, payload)
    if expect_block:
        assert rc == 2
        assert len(reasons) == 1
        assert reason_snippet is not None
        assert reason_snippet in reasons[0]
    else:
        assert rc == 0
        assert reasons == []


def test_config_protection_truncation_fail_closed_via_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DT-03 #11: 切り捨て入力は fail-closed で block する。"""
    reasons = _spy_block(monkeypatch)
    rc = _run_protection(monkeypatch, "{not-even-json", truncated=True)
    assert rc == 2
    assert len(reasons) == 1
    assert "Hook input exceeded" in reasons[0]
