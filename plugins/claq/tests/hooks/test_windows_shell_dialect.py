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
1 ファイルで並べて固定する。`docs/adr/shell-analysis-boundary.md` を参照。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, pre_bash_commit_quality
from claq.hooks.bash_config_protection import _raw_text_write_risk, find_protected_write
from claq.hooks.block_no_verify import has_bypass_flag
from claq.hooks.hook_common import command_dialect_variants


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

    @pytest.mark.parametrize(
        "command",
        [
            # sed スクリプト内の `\.` `\/`。`\/` は Windows のパス区切りに現れず、
            # POSIX 読みでも Windows 読みでも `/` は区切りのまま残る。
            r"sed -i 's/\.git\/hooks\/pre-commit//' notes.md",
            # クォート内のエスケープ空白は 1 トークンのままなので割れない。
            'rm "my\\\\ ruff.toml"',
        ],
    )
    def test_posix_escapes_do_not_become_false_positives(self, command: str) -> None:
        """Windows 読みで意味が変わらない POSIX エスケープが deny を生まないこと。

        検証の verb は必ず書き込み語彙に入っているものを使う（`touch` のように
        語彙外の verb を使うと、方言ロジックが何を返しても None になり
        テストが空回りする）。
        """
        assert find_protected_write(command) is None
        assert has_bypass_flag(command) is False

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            # PowerShell / cmd にはエスケープが無く、`my\` と `ruff.toml` は
            # 別引数になる。Remove-Item の -Path は String[] なので ruff.toml が
            # 実際に消える — POSIX 読みの「1 トークンの非保護ファイル名」とは
            # 両立しないが、docs/adr/shell-analysis-boundary.md に従い検出側へ倒す。
            (r"rm my\ ruff.toml", "ruff.toml"),
            (r"cp src.toml my\ ruff.toml", "ruff.toml"),
        ],
    )
    def test_escaped_space_is_detected_as_a_windows_write(
        self, command: str, expected: str
    ) -> None:
        """エスケープ空白は Windows では引数の区切りなので、書き込みとして検出すること。"""
        assert find_protected_write(command) == expected

    def test_escaped_space_before_bypass_flag_is_detected(self) -> None:
        """``fix\\ --no-verify`` は Windows では ``--no-verify`` が独立引数になる。

        POSIX ではコミットメッセージの一部だが、エスケープを持たないシェルでは
        git が実際にフラグとして受け取り pre-commit / commit-msg が走らない。
        """
        assert has_bypass_flag(r"git commit -m fix\ --no-verify") is True
        # クォートされていれば POSIX でも Windows でも 1 引数なので検出しない。
        assert has_bypass_flag("git commit -m 'fix --no-verify'") is False

    def test_remaining_false_positive_is_pinned(self) -> None:
        """残る誤検出を characterization test として固定する。

        ``\\`` の直後がパス構成文字なら Windows 読みを試すので、POSIX で
        「エスケープされた特殊文字」だったものが区切りに化ける場合がある。
        docs/adr/shell-analysis-boundary.md が受容する側（誤検出）の誤りであり、意図した挙動として固定する。
        ここが赤くなったら、変換規則を変えた影響が誤検出の範囲に及んでいる。
        """
        assert find_protected_write(r"rm a\.eslintrc") == ".eslintrc"


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


class TestCommandNameNormalization:
    """実行位置のコマンド名比較が 3 箇所すべてで揃っていること。

    `_executed_command_args` だけ大小無視にすると、`CD ..; rm ruff.toml` が
    docs/adr/shell-analysis-boundary.md の無条件 deny 分岐へ落ちず repo スコープ判定側へ回る（誤通過）。
    正規化を 1 関数へ集約したことを、比較箇所ごとに固定する。
    """

    def test_directory_change_is_case_insensitive(self) -> None:
        """`cd` 判定（docs/adr/shell-analysis-boundary.md の分岐）が大小を見ないこと。"""
        assert bash_config_protection._changes_working_directory(["CD", ".."]) is True
        assert bash_config_protection._changes_working_directory(["cd", ".."]) is True

    def test_command_position_wrapper_is_case_insensitive(self) -> None:
        """`sudo` 等の wrapper 読み飛ばしが大小を見ないこと。"""
        assert bash_config_protection._command_index(["SUDO", "rm", "x"]) == 1

    def test_raw_text_fallback_matches_protected_name_case_insensitively(self) -> None:
        """縮退経路で保護名も大小無視で照合すること（macOS/Windows は大小同一視）。"""
        assert _raw_text_write_risk("{broken rm RUFF.TOML") == "ruff.toml"
        assert _raw_text_write_risk("{broken cat RUFF.TOML") is None


class TestWindowsExecutableNames:
    """Windows の実行ファイル名・別名が語彙から外れないこと。"""

    @pytest.mark.parametrize(
        "command",
        [
            # `.exe` 付き。`is_git_executable_token` は既に落としているので揃える。
            "rm.exe ruff.toml",
            # cmd 組み込み / PowerShell の既定エイリアス。
            "del ruff.toml",
            "erase ruff.toml",
            "ri ruff.toml",
            "clc ruff.toml",
            "copy other.toml ruff.toml",
            "move other.toml ruff.toml",
            "sc ruff.toml -Value x",
            "ni ruff.toml",
        ],
    )
    def test_windows_write_names_are_detected(self, command: str) -> None:
        """Windows 側の実行名でも保護対象書き込みを検出すること。"""
        assert find_protected_write(command) == "ruff.toml"


class TestCommitQualityDialect:
    """`pre_bash_commit_quality` も 2 方言で commit を探すこと。

    ここだけ POSIX 読み 1 本だと、``C:\\Git\\bin\\git.exe commit -m x`` が
    `block_no_verify` では検出されるのに品質ゲートだけ素通りする非対称になる。
    """

    def test_windows_absolute_git_path_is_recognized_as_commit(self) -> None:
        """Windows の絶対パス起動でも commit と認識すること。"""
        detected = pre_bash_commit_quality._detect_git_commit(r"C:\Git\bin\git.exe commit -m x")

        assert detected is not None
        assert detected[1] == ["-m", "x"]

    def test_plain_commit_is_recognized(self) -> None:
        """通常の POSIX 起動も従来どおり検出すること。"""
        assert pre_bash_commit_quality._detect_git_commit("git commit -m x") is not None

    def test_non_commit_is_not_recognized(self) -> None:
        """commit でないコマンドは検出しないこと。"""
        assert pre_bash_commit_quality._detect_git_commit("ls -la") is None
