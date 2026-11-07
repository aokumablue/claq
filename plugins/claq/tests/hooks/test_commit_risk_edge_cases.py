"""claq.hooks.commit_risk のコミットコマンド判定のテスト（トークン化・フォールバック・-a フラグ・git commit 引数の切り出し）。"""

from __future__ import annotations

import time

import pytest

from claq.hooks import commit_risk

_MX = ["-m", "x"]
_FEAT = ["-m", "feat: x"]


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("git  commit -m x", (True, _MX), id="double-space"),
        # 解析不能で空白分割へ倒れても各トークンの引用符を外すので、正規表現 `git\s+commit` に一致しない形も拾う。
        pytest.param("echo $'\\''; \"git\" commit -m x", (True, _MX), id="quoted-git-after-unparsable"),
        # 改行はコマンド区切り。融合させるとセグメント境界に依存する判定（複数 commit・commit 前の
        # worktree 変更）が改行区切りでだけ不発になる。行内で完結する commit は検出する。
        pytest.param("git\ncommit -m x", (False, []), id="newline-between-git-and-commit"),
        pytest.param("echo hi\ngit commit -m x", (True, _MX), id="commit-on-its-own-line"),
        # グローバルオプションは既知/未知・値の有無を区別せず読み飛ばす（未知の値で打ち切ると検出漏れ）。
        pytest.param("git -C /tmp/repo commit -m x", (True, _MX), id="global-dash-c"),
        pytest.param("git --exec-path /foo commit -m x", (True, _MX), id="unknown-global-option-with-value"),
        pytest.param("git --super-prefix /x commit -am y", (True, ["-am", "y"]), id="unknown-global-option-dash-am"),
        pytest.param("echo hi && git commit -m x", (True, _MX), id="composite-command"),
        pytest.param("git status", (False, []), id="non-commit"),
        pytest.param("git git commit -m x", (True, _MX), id="repeated-git-token"),
        pytest.param("git git status", (False, []), id="repeated-git-token-without-commit"),
        pytest.param("git --version", (False, []), id="only-options-after-git"),
        # 解析できた引用文の `git commit` は実行命令ではない（`block_no_verify` と「commit とは何か」で食い違わない）。
        pytest.param("git status -m 'please run git commit later'", (False, []), id="quoted-text-parsed-cleanly"),
        # クォート不整合（heredoc 等）で shlex が失敗しても空白分割で検出を続ける。
        pytest.param("git commit -m 'unterminated quote", (True, ["-m", "unterminated", "quote"]), id="shlex-failure"),
        pytest.param("echo 'unterminated quote", (False, []), id="shlex-failure-non-commit"),
        # 解析できなかった入力にだけ、トークン走査が見つけない `git commit` を生文字列の正規表現で拾う
        # （`x-git` / `"git` は git の起動ではない。docs/adr/shell-analysis-boundary.md の過剰検出側）。
        pytest.param("echo $'\\''; echo x-git commit", (True, []), id="raw-regex-when-tokens-miss"),
        pytest.param('echo "git commit', (True, []), id="raw-regex-on-unclosed-quote"),
        # basename 化で `.exe`・絶対パス・大文字もトークン走査で捕まえる（引数が取れるので regex 頼みではない。
        # `git.exe` は `.exe` が `\s` を破るため regex からもすり抜ける）。
        pytest.param("git.exe commit -m 'feat: x'", (True, _FEAT), id="git-exe"),
        pytest.param("/usr/bin/git commit -m 'feat: x'", (True, _FEAT), id="absolute-path"),
        pytest.param("GIT commit -m 'feat: x'", (True, _FEAT), id="uppercase"),
        pytest.param("Git.EXE commit -m 'feat: x'", (True, _FEAT), id="mixed-case-exe"),
        pytest.param("'C:\\Program Files\\Git\\bin\\git.exe' commit -m 'feat: x'", (True, _FEAT), id="windows-path"),
        # git 直後の区切りで `commit` は別セグメントに孤立する。commit 後の引数収集も区切りで打ち切る。
        pytest.param("git && echo hi", (False, []), id="separator-before-commit"),
        pytest.param("git commit -m x && echo hi", (True, _MX), id="args-stop-at-and"),
        pytest.param("git commit -m x ; true", (True, _MX), id="args-stop-at-semicolon"),
        pytest.param("&& echo hi", (False, []), id="leading-separator-without-git"),
    ],
)
def test_parse_git_commit_command(command: str, expected: tuple[bool, list[str]]) -> None:
    """セグメントの実行位置にある `git commit` だけを検出し、その commit の引数を返すこと。"""
    assert commit_risk.parse_git_commit_command(command) == expected


@pytest.mark.parametrize(
    ("commit_args", "expected"),
    [(["-a", "-m", "x"], True), (["--all", "-m", "x"], True), (["-am", "x"], True), (_MX, False), (["--amend", *_MX], False)],
    ids=["short", "long", "combined", "without-all", "amend"],
)
def test_is_commit_all_flag(commit_args: list[str], expected: bool) -> None:
    """-a / --all / -am のような結合短形式だけを -a 相当と判定すること。"""
    assert commit_risk.is_commit_all_flag(commit_args) is expected


@pytest.mark.parametrize(
    "command",
    ["(printf x)>app.py;git commit -m x", "true;(rm app.py);git commit -am x", "printf x <>app.py && git commit -m x"],
    ids=["paren-redirect", "semicolon-paren", "read-write-open"],
)
def test_compound_commit_risk_sees_glued_operator_runs(command: str) -> None:
    """`shlex` が記号の連なりを 1 語にしても、commit 前の変更を見落とさない。"""
    assert commit_risk.compound_commit_risk(command) == commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE


@pytest.mark.parametrize(
    ("segment", "expected"),
    [
        pytest.param(["git", "git", "add", "x"], True, id="repeated-git-before-subcommand"),
        pytest.param(["git", "-C", ".", "add", "x"], True, id="global-option-before-subcommand"),
        pytest.param(["git", "status", "git", "add"], True, id="subcommand-after-second-git"),
        pytest.param(["git", "git", "status"], False, id="repeated-git-without-mutation"),
    ],
)
def test_segment_mutates_worktree_or_index_reads_everything_after_the_first_git(
    segment: list[str], expected: bool
) -> None:
    """最初の git より後ろのどこかに index を変えるサブコマンドがあれば、変更しうると判定する。"""
    assert commit_risk._segment_mutates_worktree_or_index(segment) is expected


def test_segment_scan_stays_linear_in_git_tokens() -> None:
    """git トークンごとに後ろ全体を読み直さないこと（16,000 語で 1.5 秒かかる二次オーダーへ戻さない）。"""
    started = time.perf_counter()
    assert commit_risk._segment_mutates_worktree_or_index(["git"] * 32_000) is False
    assert time.perf_counter() - started < 1.0


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("bash -c 'git commit -m x'", (True, _MX), id="one-level"),
        pytest.param("BASH -lc 'git commit -m x'", (True, _MX), id="normalized-shell-name"),
        pytest.param("echo ok; sh -c 'git commit -m x'", (True, _MX), id="after-a-separator"),
        pytest.param("bash -c 'git status'", (False, []), id="no-commit-inside"),
        # 2 段以上の ``sh -c`` は非目標（docs/adr/shell-analysis-boundary.md）。
        pytest.param("""bash -c "bash -c 'git commit -m x'" """, (False, []), id="two-levels"),
        # 内側の heredoc のデータ本文は `block_no_verify` の再帰と同じく実行命令として読まない。
        pytest.param("""bash -c "cat <<'EOF'\ngit commit -m x\nEOF" """, (False, []), id="heredoc-data-inside"),
    ],
)
def test_parse_git_commit_command_reads_one_shell_wrapper(command: str, expected: tuple[bool, list[str]]) -> None:
    """``sh -c`` の 1 段の内側の commit も commit と読む。"""
    assert commit_risk.parse_git_commit_command(command) == expected


def test_parse_git_commit_command_can_leave_shell_wrappers_unread() -> None:
    """argv の結合候補は ``sh -c`` の内側を読まない（内側の commit は要素側で数える）。"""
    assert commit_risk.parse_git_commit_command("bash -c 'git commit -m x'", shell_wrappers=False) == (False, [])


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("bash -c 'git add x && git commit -m y'", "mutation", id="mutation-inside"),
        pytest.param("git add x; bash -c 'git commit -m y'", "mutation", id="mutation-before-the-wrapper"),
        pytest.param("bash -c 'git add x'; git commit -m y", "mutation", id="wrapper-mutates-before-commit"),
        pytest.param("bash -c 'git commit -m a'; git commit -m b", "multiple", id="two-commits"),
        pytest.param("bash -c 'git commit -m a && git commit -m b'", "multiple", id="two-commits-inside"),
        pytest.param("bash -c 'git commit -m y'", None, id="single-commit"),
        pytest.param("bash -c 'git commit -m y' >/tmp/log", None, id="redirect-of-the-wrapper-itself"),
    ],
)
def test_compound_commit_risk_reads_one_shell_wrapper(command: str, expected: str | None) -> None:
    """``sh -c`` の 1 段の内側も、複数 commit と commit 前の変更の判定に数える。"""
    messages = {
        "multiple": commit_risk.MULTIPLE_COMMITS_MESSAGE,
        "mutation": commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE,
        None: None,
    }
    assert commit_risk.compound_commit_risk(command) == messages[expected]
