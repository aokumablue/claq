"""claq.hooks.commit_risk のコミットコマンド判定のテスト（トークン化・フォールバック・-a フラグ・git commit 引数の切り出し）。"""

from __future__ import annotations

import pytest

from claq.hooks import commit_risk as commit_risk

# ─────────────────────────────────────────────
# _is_git_commit_command / _is_commit_all_flag（トークン化）
# ─────────────────────────────────────────────


def test_is_git_commit_command_detects_double_space() -> None:
    """連続空白（git  commit）でも commit 検出が成立すること。"""
    is_commit, args = commit_risk._is_git_commit_command("git  commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_detects_quoted_git_after_unparsable_input() -> None:
    """解析不能な入力の後でも、引用符付きの `git` / `commit` を commit として検出すること。

    空白分割へ倒れても各トークンから引用符を取り除くため、生文字列の正規表現
    （`git\\s+commit`）に一致しない `"git" commit` が検出から漏れない。
    """
    is_commit, args = commit_risk._is_git_commit_command("echo $'\\''; \"git\" commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_treats_newline_as_a_command_separator() -> None:
    """改行はコマンド区切りなので `git` 改行 `commit` は commit ではない。

    シェルは `git` を引数なしで実行してから `commit` という別コマンドを実行する。
    これを 1 つの `git commit` として融合させると、セグメント境界に依存する判定
    （複数 commit・commit 前の worktree 変更）がまとめて不発になり、
    `printf x > s.py` 改行 `git add s.py` 改行 `git commit -m x` が通るのに
    `;` 区切りの同内容だけが止まる。
    """
    is_commit, _args = commit_risk._is_git_commit_command("git\ncommit -m x")
    assert is_commit is False

    # 各行が独立したコマンドとして読まれるため、行内で完結する commit は検出する。
    is_commit, args = commit_risk._is_git_commit_command("echo hi\ngit commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_global_dash_c_option() -> None:
    """`git -C <path> commit` のようなグローバルオプション付きでも検出できること。"""
    is_commit, args = commit_risk._is_git_commit_command("git -C /tmp/repo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_with_value() -> None:
    """allowlist に無いグローバルオプション（--exec-path 等）でも検出漏れしないこと。

    既知オプション（-C/-c 等）のみ値トークンをスキップすると、
    `--exec-path <path>` のような未知オプションの値トークンで走査が
    打ち切られ検出漏れになる（フェイルセーフ違反）。そのため
    オプション・値を区別せず commit まで読み飛ばす。
    """
    is_commit, args = commit_risk._is_git_commit_command("git --exec-path /foo commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_skips_unknown_global_option_detects_dash_a() -> None:
    """未知グローバルオプション経由でも -a 相当のフラグが検出できること。"""
    is_commit, args = commit_risk._is_git_commit_command("git --super-prefix /x commit -am y")
    assert is_commit is True
    assert commit_risk._is_commit_all_flag(args) is True


def test_is_git_commit_command_detects_within_composite_command() -> None:
    """`&&` で連結された複合コマンド中の git commit も検出できること。"""
    is_commit, args = commit_risk._is_git_commit_command("echo hi && git commit -m x")
    assert is_commit is True
    assert args == ["-m", "x"]


def test_is_git_commit_command_returns_false_for_non_commit() -> None:
    """git commit を含まないコマンドは False を返すこと。"""
    is_commit, args = commit_risk._is_git_commit_command("git status")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_falls_back_on_shlex_failure() -> None:
    """クォート不整合（heredoc 等）で shlex.split が失敗しても検出を継続すること。"""
    broken_command = "git commit -m 'unterminated quote"
    is_commit, _args = commit_risk._is_git_commit_command(broken_command)
    assert is_commit is True


def test_is_git_commit_command_raw_regex_still_applies_when_tokens_miss_it() -> None:
    """解析不能な入力では、トークン列が commit を見つけられなくても生文字列の正規表現で検出すること。

    ``x-git`` は git の起動ではないためトークン経由では commit にならないが、解析できない
    入力に対するフェイルセーフとして ``git commit`` の語が見えた時点で commit とみなす。
    """
    assert commit_risk._is_git_commit_command("echo $'\\''; echo x-git commit") == (True, [])


def test_is_git_commit_command_fallback_non_commit_returns_false() -> None:
    """フォールバック経路でも git commit を含まなければ False を返すこと。"""
    broken_command = "echo 'unterminated quote"
    is_commit, args = commit_risk._is_git_commit_command(broken_command)
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_returns_false_when_only_options_follow_git() -> None:
    """git の後がオプションのみで commit が現れなければ False を返すこと。"""
    is_commit, args = commit_risk._is_git_commit_command("git --version")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_trusts_clean_tokenization() -> None:
    """解析できた引用文の中の `git commit` は実行命令とみなさないこと。

    生文字列への regex を解析成功時にも当てると、`copilot -p '... git
    commit ...'` のような引用文まで commit と判定し、無関係な Bash 呼び出しが
    index の状態次第でブロックされる。同じ入力を `block_no_verify` は無視する
    ので、2 つのフックが「commit とは何か」で食い違うことにもなる。
    """
    is_commit, args = commit_risk._is_git_commit_command("git status -m 'please run git commit later'")
    assert is_commit is False
    assert args == []


def test_is_git_commit_command_regex_fallback_applies_when_tokenization_fails() -> None:
    """クォート不整合で解析できなかった入力にだけ regex フォールバックが効くこと。

    解析できない構文に対しては docs/adr/shell-analysis-boundary.md どおり過剰検出側へ倒す。
    """
    # クォート不整合で shlex が失敗し、空白分割後のトークン `"git` は git 実行
    # ファイルとして認識されない。生文字列の regex だけが commit を見つける。
    is_commit, args = commit_risk._is_git_commit_command('echo "git commit')
    assert is_commit is True
    assert args == []


@pytest.mark.parametrize(
    "command",
    [
        "git.exe commit -m 'feat: x'",
        "/usr/bin/git commit -m 'feat: x'",
        "GIT commit -m 'feat: x'",
        "Git.EXE commit -m 'feat: x'",
        "'C:\\Program Files\\Git\\bin\\git.exe' commit -m 'feat: x'",
    ],
)
def test_is_git_commit_command_detects_exe_and_absolute_path_tokens(command: str) -> None:
    """basename 化により `.exe`・絶対パス・大文字表記の git 実行ファイルもトークン走査で検出する。

    `token != "git"` の完全一致では、`/usr/bin/git` は regex
    フォールバックにのみ救われ、`git.exe` は `.exe` が `\\s` を破るため
    regex フォールバックからもすり抜ける。
    """
    is_commit, _args = commit_risk._is_git_commit_command(command)
    assert is_commit is True


def test_is_git_commit_command_exe_token_walk_not_regex_fallback() -> None:
    """`git.exe commit` はトークン走査自体で捕まる（regex フォールバック頼みではない）。"""
    args = commit_risk._find_git_commit_args_in_segment(["git.exe", "commit", "-m", "x"])
    assert args == ["-m", "x"]


def test_is_commit_all_flag_detects_short_long_and_combined() -> None:
    """-a / --all / -am のような結合短形式を検出すること。"""
    assert commit_risk._is_commit_all_flag(["-a", "-m", "x"]) is True
    assert commit_risk._is_commit_all_flag(["--all", "-m", "x"]) is True
    assert commit_risk._is_commit_all_flag(["-am", "x"]) is True
    assert commit_risk._is_commit_all_flag(["-m", "x"]) is False
    assert commit_risk._is_commit_all_flag(["--amend", "-m", "x"]) is False


def test_find_git_commit_args_stops_at_separator_before_commit() -> None:
    """git の直後にシェル区切りが現れた場合、その git 呼び出しは commit と
    みなさないこと（区切り記号でセグメントが分かれ、`commit` が別セグメントに
    孤立するため見つからない分岐）。
    """
    is_commit, args = commit_risk._is_git_commit_command("git && echo hi")
    assert is_commit is False
    assert args == []


def test_git_commit_args_stop_at_shell_operator() -> None:
    """`git commit` 後の引数収集は `&&` / `;` / `|` 等のセグメント境界で打ち切る。"""
    is_commit, args = commit_risk._is_git_commit_command("git commit -m x && echo hi")
    assert is_commit is True
    assert args == ["-m", "x"]

    is_commit, args = commit_risk._is_git_commit_command("git commit -m x ; true")
    assert is_commit is True
    assert args == ["-m", "x"]

    is_commit, args = commit_risk._is_git_commit_command("&& echo hi")
    assert is_commit is False
    assert args == []


@pytest.mark.parametrize(
    "command",
    [
        "(printf x)>app.py;git commit -m x",
        "true;(rm app.py);git commit -am x",
        "printf x <>app.py && git commit -m x",
    ],
    ids=["paren-redirect", "semicolon-paren", "read-write-open"],
)
def test_compound_commit_risk_sees_glued_operator_runs(command: str) -> None:
    """`shlex` が記号の連なりを 1 語にしても、commit 前の変更を見落とさない。"""
    assert commit_risk._compound_commit_risk(command) == commit_risk._MUTATION_BEFORE_COMMIT_MESSAGE
