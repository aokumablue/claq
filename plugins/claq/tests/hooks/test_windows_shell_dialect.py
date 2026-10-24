"""Windows のシェル方言（PowerShell / cmd）で書かれたコマンドの検出テスト。

Windows ホストのシェルツールは PowerShell であり、次の 2 点で POSIX 前提の
解析からずれる:

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

import pytest

from claq.hooks import bash_config_protection, pre_bash_commit_quality
from claq.hooks.bash_config_protection import _raw_text_write_risk, find_protected_write
from claq.hooks.block_no_verify import has_bypass_flag
from claq.hooks.shell_tokens import command_dialect_variants

from ._support import _repo_scope as _repo_scope


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # バックスラッシュが無ければ読み方は 1 通りだけ（余計な検査をしない）。
        ("rm ruff.toml", ("rm ruff.toml",)),
        (r"rm .\ruff.toml", (r"rm .\ruff.toml", "rm ./ruff.toml")),
    ],
)
def test_command_dialect_variants(command: str, expected: tuple[str, ...]) -> None:
    """バックスラッシュを含むときだけ Windows 読み（区切り扱い）を加える。"""
    assert command_dialect_variants(command) == expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # POSIX 読みで潰れる `\` 区切りパスを Windows 読みで捕まえる。
        (r"rm .\.eslintrc", ".eslintrc"),
        (r"rm C:\repo\.eslintrc", ".eslintrc"),
        (r"Remove-Item .\.eslintrc", ".eslintrc"),
        # PowerShell / cmd にはエスケープが無く `my\` と `ruff.toml` は別引数になる（Remove-Item の -Path は
        # String[]）。POSIX 読みとは両立しないが、docs/adr/shell-analysis-boundary.md に従い検出側へ倒す。
        (r"rm my\ ruff.toml", "ruff.toml"),
        (r"cp src.toml my\ ruff.toml", "ruff.toml"),
        # 残る誤検出の characterization。`\` の直後がパス構成文字なら Windows 読みを試すので、POSIX で
        # エスケープされた特殊文字が区切りに化ける（ADR が受容する側の誤り）。赤くなったら変換規則の
        # 変更が誤検出の範囲に及んでいる。
        (r"rm a\.eslintrc", ".eslintrc"),
        # PowerShell の長形式 cmdlet。cmdlet 名は大小無視で解決される。読み取り専用 cmdlet は過剰検出の歯止め。
        ("Remove-Item ruff.toml", "ruff.toml"),
        ("Clear-Content ruff.toml", "ruff.toml"),
        ("Set-Content ruff.toml -Value x", "ruff.toml"),
        ("Add-Content ruff.toml -Value x", "ruff.toml"),
        ("Out-File -FilePath ruff.toml", "ruff.toml"),
        ("New-Item ruff.toml", "ruff.toml"),
        ("Copy-Item other.toml ruff.toml", "ruff.toml"),
        ("Move-Item ruff.toml other.toml", "ruff.toml"),
        ("REMOVE-ITEM ruff.toml", "ruff.toml"),
        ("Get-Content ruff.toml", None),
        # `.exe` 付き（`is_git_executable_token` と揃える）と cmd 組み込み / PowerShell の既定エイリアス。
        ("rm.exe ruff.toml", "ruff.toml"),
        ("del ruff.toml", "ruff.toml"),
        ("erase ruff.toml", "ruff.toml"),
        ("ri ruff.toml", "ruff.toml"),
        ("clc ruff.toml", "ruff.toml"),
        ("copy other.toml ruff.toml", "ruff.toml"),
        ("move other.toml ruff.toml", "ruff.toml"),
        ("sc ruff.toml -Value x", "ruff.toml"),
        ("ni ruff.toml", "ruff.toml"),
    ],
)
def test_windows_write_is_detected(command: str, expected: str | None) -> None:
    """Windows のパス区切り・cmdlet・実行名でも保護対象への書き込みを名指しする。"""
    assert find_protected_write(command) == expected


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
def test_posix_escapes_do_not_become_false_positives(command: str) -> None:
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
        # POSIX 読みでは `C:Gitbingit.exe` に潰れ、`is_git_executable_token` が git と認識できなかった。
        (r"C:\Git\bin\git.exe commit --no-verify -m x", True),
        # エスケープを持たないシェルでは `--no-verify` が独立引数になり、git がフラグとして受け取る。
        (r"git commit -m fix\ --no-verify", True),
        # クォートされていれば POSIX でも Windows でも 1 引数。
        ("git commit -m 'fix --no-verify'", False),
    ],
)
def test_bypass_flag_in_windows_reading(command: str, expected: bool) -> None:
    """Windows 読みで git 起動・独立引数になるバイパスフラグを検出する。"""
    assert has_bypass_flag(command) is expected


@pytest.mark.parametrize(
    ("raw_input", "expected"),
    [
        # 指標を小文字で持つため、生テキスト側も大小を無視しないと「JSON が壊れているときだけ
        # `Remove-Item` が通る」非対称が残る。保護名も大小無視（macOS/Windows は大小同一視）。
        ("{broken Remove-Item ruff.toml", "ruff.toml"),
        ("{broken Get-Content ruff.toml", None),
        ("{broken rm RUFF.TOML", "ruff.toml"),
        ("{broken cat RUFF.TOML", None),
    ],
)
def test_malformed_input_fallback_matches_case_insensitively(raw_input: str, expected: str | None) -> None:
    """JSON が壊れている縮退経路でも長形式 cmdlet と大文字の保護名を拾う。"""
    assert _raw_text_write_risk(raw_input) == expected


def test_command_name_comparisons_are_case_insensitive() -> None:
    """実行位置のコマンド名比較（`cd` 判定・wrapper 読み飛ばし）が大小を見ないこと。

    書き込み先の抽出（`_EXECUTED_COMMAND_TARGET_RULES`）だけ大小無視にすると、`CD ..; rm ruff.toml` が
    docs/adr/shell-analysis-boundary.md の無条件 deny 分岐へ落ちず repo スコープ判定側へ回る（誤通過）。
    """
    assert bash_config_protection._changes_working_directory(["CD", ".."]) is True
    assert bash_config_protection._changes_working_directory(["cd", ".."]) is True
    assert bash_config_protection._command_index(["SUDO", "rm", "x"]) == 1


@pytest.mark.parametrize(
    ("command", "expected_args"),
    [(r"C:\Git\bin\git.exe commit -m x", ["-m", "x"]), ("git commit -m x", ["-m", "x"]), ("ls -la", None)],
    ids=["windows-absolute-git", "posix-git", "non-commit"],
)
def test_commit_quality_detects_commit_in_both_dialects(command: str, expected_args: list[str] | None) -> None:
    """`pre_bash_commit_quality` も 2 方言で commit を探す。

    POSIX 読み 1 本だと、Windows の絶対パス起動が `block_no_verify` では検出されるのに
    品質ゲートだけ素通りする非対称になる。
    """
    detected = pre_bash_commit_quality._detect_git_commit(command)

    assert (None if detected is None else detected[1]) == expected_args
