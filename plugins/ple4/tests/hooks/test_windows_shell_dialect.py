"""Windows のシェル方言（PowerShell / cmd）で書かれたコマンドの検出テスト。

Windows ホストのシェルツールは PowerShell であり、次の 2 点で POSIX 前提の
解析からずれる（release-verify 2026-09-03 の再レビューで実測）:

1. `\\` はエスケープ記号ではなくパス区切り。`shlex(posix=True)` は
   `rm .\\.eslintrc` を `['rm', '..eslintrc']` に潰し、保護対象 basename も
   git 起動トークンも見失う。
2. `rm`/`cp`/`mv` は PowerShell の別名として同じ cmdlet に解決されるので既に
   効くが、長形式（`Remove-Item` / `Set-Content` / `Out-File` 等）は語彙に
   無かった。

いずれも「Windows では保護 hook が素通りする」という同じ帰結になるため、
1 ファイルで並べて固定する。`docs/adr/0020-*.md` を参照。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ple4.hooks import bash_config_protection
from ple4.hooks.bash_config_protection import _raw_text_write_risk, find_protected_write
from ple4.hooks.block_no_verify import has_bypass_flag
from ple4.hooks.hook_common import command_dialect_variants


@pytest.fixture(autouse=True)
def _fixed_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """repo スコープ判定（A-06）を tmp_path 配下に固定する。

    `find_protected_write` は保護対象ヒット後にリポジトリルート配下かどうかを
    見る。`resolve_repo_root` は `lru_cache` 付きで、実行環境の git 状態や
    他テストのキャッシュに左右されるため、cwd と併せて固定する。
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
    return tmp_path


class TestCommandDialectVariants:
    """`command_dialect_variants`（2 方言の読み方）の契約。"""

    def test_command_without_backslash_has_one_reading(self) -> None:
        """バックスラッシュが無ければ読み方は 1 通りだけ（余計な検査をしない）。"""
        assert command_dialect_variants("rm ruff.toml") == ("rm ruff.toml",)

    def test_backslash_command_adds_a_windows_reading(self) -> None:
        """バックスラッシュを含むなら Windows 読み（区切り扱い）も加える。"""
        assert command_dialect_variants(r"rm .\ruff.toml") == (
            r"rm .\ruff.toml",
            "rm ./ruff.toml",
        )


class TestWindowsPathSeparators:
    """`\\` 区切りのパスでも保護対象と git 起動を見失わないこと。"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            (r"rm .\.eslintrc", ".eslintrc"),
            (r"rm C:\repo\.eslintrc", ".eslintrc"),
            (r"Remove-Item .\.eslintrc", ".eslintrc"),
        ],
    )
    def test_backslash_paths_are_still_detected(self, command: str, expected: str) -> None:
        """POSIX 読みで潰れる `\\` 区切りパスを Windows 読みで捕まえること。"""
        assert find_protected_write(command) == expected

    def test_absolute_windows_git_path_is_still_a_git_invocation(self) -> None:
        """`C:\\...\\git.exe` 形式の絶対パス起動でもバイパスフラグを検出すること。

        POSIX 読みでは `C:Gitbingit.exe` に潰れ、`is_git_executable_token` が
        git と認識できなかった。
        """
        assert has_bypass_flag(r"C:\Git\bin\git.exe commit --no-verify -m x") is True

    def test_posix_escape_does_not_become_a_false_positive(self) -> None:
        """POSIX のエスケープ（`my\\ file.txt`）が新たな deny を生まないこと。

        Windows 読みでは `my/ file.txt` になるが basename は `file.txt` で、
        保護対象名ではないため検出されない。
        """
        assert find_protected_write(r"touch my\ file.txt") is None


class TestPowerShellCmdlets:
    """PowerShell の長形式 cmdlet が書き込み語彙に入っていること。"""

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("Remove-Item ruff.toml", "ruff.toml"),
            ("Clear-Content ruff.toml", "ruff.toml"),
            ("Set-Content ruff.toml -Value x", "ruff.toml"),
            ("Add-Content ruff.toml -Value x", "ruff.toml"),
            ("Out-File -FilePath ruff.toml", "ruff.toml"),
            ("New-Item ruff.toml", "ruff.toml"),
            ("Copy-Item other.toml ruff.toml", "ruff.toml"),
            ("Move-Item ruff.toml other.toml", "ruff.toml"),
        ],
    )
    def test_write_cmdlets_are_detected(self, command: str, expected: str) -> None:
        """書き込み系 cmdlet の対象が保護対象なら deny になること。"""
        assert find_protected_write(command) == expected

    def test_cmdlet_names_are_matched_case_insensitively(self) -> None:
        """PowerShell は cmdlet 名を大小無視で解決するため、判定も大小を見ない。"""
        assert find_protected_write("REMOVE-ITEM ruff.toml") == "ruff.toml"

    def test_read_only_cmdlet_is_not_detected(self) -> None:
        """読み取り専用の cmdlet は書き込みとみなさないこと（過剰検出の歯止め）。"""
        assert find_protected_write("Get-Content ruff.toml") is None

    def test_malformed_input_fallback_also_matches_cmdlets(self) -> None:
        """JSON が壊れている縮退経路でも長形式 cmdlet を拾うこと。

        指標を小文字で持つようになったため、生テキスト側も大小を無視しないと
        「JSON が壊れているときだけ `Remove-Item` が通る」非対称が残る。
        """
        assert _raw_text_write_risk("{broken Remove-Item ruff.toml") == "ruff.toml"
        assert _raw_text_write_risk("{broken Get-Content ruff.toml") is None
