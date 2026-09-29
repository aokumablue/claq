"""config_protection / bash_config_protection の symlink 経由バイパス回帰テスト（H-02）。

`alias -> pyproject.toml` のような symlink 経由の書込みは、raw path の
basename（``alias``）だけを見る判定をすり抜けていた（v0.9.34 監査 H-02）。
両モジュールが `hook_common.resolve_effective_target` で実体 path を解決
してから basename 判定することを、Edit/Write（`config_protection`）と
Bash 直接書込み（`bash_config_protection`）の両経路で確認する。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, config_protection


def _write_payload(file_path: str, content: str = "x") -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


class TestConfigProtectionSymlink:
    """Edit/Write 経路（config_protection）の symlink 解決テスト。"""

    def test_blocks_symlink_to_unconditionally_protected_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """alias -> .ruff.toml への Write は実体の basename で deny する。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / ".ruff.toml").write_text("", encoding="utf-8")
        (tmp_path / "alias-file").symlink_to(tmp_path / ".ruff.toml")
        monkeypatch.setattr(
            config_protection, "read_raw_stdin_with_truncation", lambda: (json.dumps(_write_payload("alias-file")), False)
        )

        assert config_protection.main() == 2

    def test_allows_symlink_to_unprotected_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """symlink 先が保護対象でなければ allow する。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "README.md").write_text("", encoding="utf-8")
        (tmp_path / "alias-safe").symlink_to(tmp_path / "README.md")
        monkeypatch.setattr(
            config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (json.dumps(_write_payload("alias-safe")), False),
        )

        assert config_protection.main() == 0

    def test_broken_symlink_does_not_crash_and_allows(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """壊れた symlink は解決不能として raw basename にフォールバックし、クラッシュしない。"""
        monkeypatch.chdir(tmp_path)
        (tmp_path / "broken-link").symlink_to(tmp_path / "does-not-exist")
        monkeypatch.setattr(
            config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (json.dumps(_write_payload("broken-link")), False),
        )

        assert config_protection.main() == 0

    def test_still_blocks_direct_protected_path_without_symlink(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """symlink を介さない直接 path 指定は従来どおり deny する（回帰防止）。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (json.dumps(_write_payload(".ruff.toml")), False),
        )

        assert config_protection.main() == 2

    def test_effective_basename_falls_back_when_resolve_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """resolve_effective_target が None を返す場合、raw path の basename にフォールバックする。"""
        monkeypatch.setattr(config_protection, "resolve_effective_target", lambda _path: None)

        assert config_protection._effective_basename(".ruff.toml") == ".ruff.toml"


class TestBashConfigProtectionSymlink:
    """Bash 直接書込み経路（bash_config_protection）の symlink 解決テスト。"""

    @pytest.fixture(autouse=True)
    def _fixed_repo_root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
        return tmp_path

    def test_printf_redirect_through_symlink_is_blocked(self, tmp_path: Path) -> None:
        (tmp_path / ".ruff.toml").write_text("", encoding="utf-8")
        (tmp_path / "alias-file").symlink_to(tmp_path / ".ruff.toml")

        found = bash_config_protection.find_protected_write("printf x > alias-file")

        assert found == ".ruff.toml"

    def test_tee_through_symlink_is_blocked(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.ruff]\n", encoding="utf-8")
        (tmp_path / "alias-file").symlink_to(tmp_path / "pyproject.toml")

        # pyproject.toml は conditionally-protected ではなく find_protected_write レベルでは
        # basename 一致のみ判定するため、確実に unconditional な .ruff.toml で検証する。
        (tmp_path / ".ruff.toml").write_text("", encoding="utf-8")
        (tmp_path / "alias-tee").symlink_to(tmp_path / ".ruff.toml")

        found = bash_config_protection.find_protected_write("tee alias-tee <<< 'x'")

        assert found == ".ruff.toml"

    def test_symlink_to_unprotected_target_is_allowed(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("", encoding="utf-8")
        (tmp_path / "alias-safe").symlink_to(tmp_path / "notes.txt")

        assert bash_config_protection.find_protected_write("printf x > alias-safe") is None

    def test_symlink_target_outside_repo_root_is_allowed(self, tmp_path: Path) -> None:
        """A-06 の契約を維持: 解決後 target がリポジトリ外なら allow。"""
        outside_dir = tmp_path.parent / "outside-repo-root-fixture"
        outside_dir.mkdir(exist_ok=True)
        outside_target = outside_dir / ".ruff.toml"
        outside_target.write_text("", encoding="utf-8")
        (tmp_path / "alias-outside").symlink_to(outside_target)

        assert bash_config_protection.find_protected_write("printf x > alias-outside") is None

    def test_broken_symlink_falls_back_and_allows(self, tmp_path: Path) -> None:
        (tmp_path / "broken-link").symlink_to(tmp_path / "does-not-exist")

        assert bash_config_protection.find_protected_write("printf x > broken-link") is None
