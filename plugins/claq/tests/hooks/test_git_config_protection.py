"""git config 経由のフック迂回（hooksPath・include・alias）に対する保護のテスト。

Edit/Write は置換後の全文を解析し、フック実行を左右する key が置換前に無かった・値が変わった
ときだけ deny する。remote / branch / user の通常編集は通す。Bash からの書込みは内容を解釈せず
deny する。key 集合は `block_no_verify` が `git config` 経由で塞ぐものと同じ。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, block_no_verify, config_protection

_REMOTE_SECTION = '[remote "origin"]\n\turl = git@example.com:o/r.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
_HOOKS_PATH_SECTION = "[core]\n\thooksPath = /dev/null\n"
_ALIAS_CONFIG = "[core]\n\tbare = false\n[alias]\n\tci = commit\n" + _REMOTE_SECTION
_TWO_ALIASES = "[alias]\n\ta = x\n\tb = x\n"
_USER = "[user]\n\tname = x\n"
# 走査上限（_LINT_SIGNAL_MAX_TEXT_BYTES）を超える長さの本文。
_OVER_CAP = "# pad\n" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES // 6 + 10)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """cwd・ホーム・XDG を tmp_path 配下へ固定し、ホームのパスを返す。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAQ_HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


def _run(monkeypatch: pytest.MonkeyPatch, payload: dict) -> int:
    """config_protection.main() を実行し終了コードを返す。"""
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (json.dumps(payload), False))
    return config_protection.main()


def _edit(old_string: str, new_string: str, *, file_path: str = ".git/config", replace_all: bool = False) -> dict:
    """Edit ツールの payload を作る。"""
    tool_input = {"file_path": file_path, "old_string": old_string, "new_string": new_string}
    if replace_all:
        tool_input["replace_all"] = True
    return {"tool_name": "Edit", "tool_input": tool_input}


def _write(content: str, *, file_path: str = ".git/config") -> dict:
    """Write ツールの payload を作る。"""
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


def _multi_edit(edits: object) -> dict:
    """`.git/config` への MultiEdit の payload を作る。"""
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": ".git/config", "edits": edits}}


def _put(path: str | Path, text: str) -> Path:
    """親ディレクトリごと作ってファイルを置く。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# (ディスク上の `.git/config` の現在の内容, payload, 期待 exit code)。
# 内容が None の行はファイルを置かない。bytes の行は改行を変換せずにそのまま置く。
_FILE_CASES = [
    # --- Edit はディスク上の全文へ置換を当てて判定する ---
    pytest.param(
        _ALIAS_CONFIG,
        _edit("\tci = commit\n", "\tci = commit\n\tpush = push --no-verify\n"),
        2,
        id="fragment-into-existing-alias-section",
    ),
    pytest.param(
        _ALIAS_CONFIG, _edit(_REMOTE_SECTION, _REMOTE_SECTION + '[remote "up"]\n\turl = u\n'), 0, id="edit-adding-remote"
    ),
    pytest.param(
        _ALIAS_CONFIG, _edit("git@example.com:o/r.git", "git@example.com:o/new.git"), 0, id="edit-changing-remote-url"
    ),
    pytest.param(
        _ALIAS_CONFIG,
        _edit("\tbare = false\n", "\tbare = false\n" + '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'),
        0,
        id="edit-adding-branch",
    ),
    pytest.param(_ALIAS_CONFIG, _edit("ci = commit\n", "ci = commit --no-verify\n"), 2, id="edit-changing-alias-value"),
    pytest.param(
        _ALIAS_CONFIG,
        _edit("\tbare = false\n", "\tbare = false\n\thooksPath = /dev/null\n"),
        2,
        id="edit-adding-hooks-path-into-existing-core",
    ),
    # 見出しだけを `[alias]` に直して既存 key を alias 扱いにする。
    pytest.param("[aliases]\n\tci = commit --no-verify\n", _edit("[aliases]", "[alias]"), 2, id="header-rename-into-alias"),
    pytest.param(_USER, _edit("[user]", "[aliases]\n\tx = y\n[user]"), 0, id="aliases-heading"),
    pytest.param(
        '[submodule "lib"]\n\turl = u\n',
        _edit("url = u\n", "url = u\n\tpath = vendor/lib\n"),
        0,
        id="path-key-in-non-include-section",
    ),
    # 後ろの上書きだけを消して危険な値を実効にする。
    pytest.param(
        "[alias]\n\tci = commit --no-verify\n\tci = commit\n", _edit("\tci = commit\n", ""), 2, id="removing-shadowing-alias"
    ),
    pytest.param(_ALIAS_CONFIG, _edit("[alias]\n\tci = commit\n", ""), 0, id="removing-alias"),
    pytest.param(
        _TWO_ALIASES, _edit("= x", "= x --no-verify", replace_all=True), 2, id="replace-all-applies-every-occurrence"
    ),
    pytest.param(_TWO_ALIASES, _edit("a = x", "a = x --no-verify"), 2, id="single-alias-value-changed"),
    pytest.param(
        _USER + '[remote "o"]\n\turl = x\n', _edit("= x", "= y"), 0, id="multiple-occurrences-replace-first-only"
    ),
    # 内容のあるファイルで置換対象が見つからない・空の old_string は、実ツールとの食い違いを排除できないため deny。
    pytest.param(_ALIAS_CONFIG, _edit("no such text", _REMOTE_SECTION), 2, id="old-string-not-found-in-existing-file"),
    pytest.param(_ALIAS_CONFIG, _edit("", _REMOTE_SECTION), 2, id="empty-old-string-on-existing-file"),
    # 単独の CR を改行に変換せずに読む。変換すると、コメントの一部だった行を危険 key の既存行と誤認する。
    pytest.param(b"[user]\n# x\r[core] hooksPath = /e\n", _edit("# x", ""), 2, id="lone-carriage-return-not-converted"),
    pytest.param(
        b"[alias]\r\n\tci = commit\r\n[user]\r\n\tname = x\r\n", _edit("name = x", "name = y"), 0, id="crlf-unrelated-edit"
    ),
    # ファイルが無い・空なら new_string が全文になる。
    pytest.param("", _edit("", _REMOTE_SECTION), 0, id="empty-file-uses-new-string"),
    pytest.param(None, _edit("", _REMOTE_SECTION), 0, id="missing-file-uses-new-string"),
    pytest.param(None, _edit("x", _HOOKS_PATH_SECTION), 2, id="missing-file-new-string-sensitive"),
    # --- Write ---
    pytest.param(
        _ALIAS_CONFIG, _write(_ALIAS_CONFIG + '[branch "m"]\n\tremote = o\n'), 0, id="rewriting-identical-alias"
    ),
    # 解析できない全文は deny する。
    pytest.param(None, _write("[user\n"), 2, id="unparsable-write"),
    pytest.param(None, _edit("", "\thooksPath = /dev/null"), 2, id="unparsable-edit-result"),
    # 解決できないパス（NUL 混入）でも、new_string を全文として判定する。
    pytest.param(None, _write(_HOOKS_PATH_SECTION, file_path="\x00/.git/config"), 2, id="unresolvable-target-sensitive"),
    pytest.param(None, _write(_USER, file_path="\x00/.git/config"), 0, id="unresolvable-target-ordinary"),
    # 置換前が解析できなくても、置換後が解析できて危険 key が無ければ通す。
    pytest.param("garbage\n", _write(_USER), 0, id="unparsable-existing-file-ordinary-write"),
    # 内容が取れない書込みは他の条件付き保護対象と同じく fail-closed にする。
    pytest.param(None, {"tool_name": "Write", "tool_input": {"file_path": ".git/config"}}, 2, id="unverifiable-content"),
    # パッチ本文からは全文を作れないため deny する。
    pytest.param(
        None,
        {"tool_name": "apply_patch", "tool_input": {"input": "*** Update File: .git/config\n+[user]\n"}},
        2,
        id="apply-patch",
    ),
    # 走査上限を超える全文は走査せず deny する。置換前が超える場合も、上限より後ろへの追記を見逃さない。
    pytest.param(None, _write(_OVER_CAP), 2, id="over-cap-result"),
    pytest.param(_OVER_CAP + _USER, _edit("name = x", "name = y"), 2, id="over-cap-existing-file"),
    # --- MultiEdit は編集を順に当てた全文で判定する ---
    pytest.param(
        _USER,
        _multi_edit([
            {"old_string": "[user]", "new_string": "[alias]"},
            {"old_string": "name = x", "new_string": "name = commit --no-verify"},
        ]),
        2,
        id="multiedit-sequential-edits-compose",
    ),
    pytest.param(
        _ALIAS_CONFIG,
        _multi_edit([
            {"old_string": "bare = false", "new_string": "bare = true"},
            {"old_string": "r.git", "new_string": "r2.git"},
        ]),
        0,
        id="multiedit-ordinary-edits",
    ),
    pytest.param(
        _TWO_ALIASES,
        _multi_edit([{"old_string": "= x", "new_string": "= y", "replace_all": True}]),
        2,
        id="multiedit-replace-all-per-edit",
    ),
    # 内容のあるファイルで適用できない編集が 1 件でもあれば deny する。
    pytest.param(
        _ALIAS_CONFIG,
        _multi_edit([
            {"old_string": "bare = false", "new_string": "bare = true"},
            {"old_string": "missing", "new_string": _USER},
        ]),
        2,
        id="multiedit-unapplicable-edit-on-existing-file",
    ),
    # ファイルが無いときは全編集の new_string を連結した全文で判定する。
    pytest.param(
        None,
        _multi_edit([{"old_string": "", "new_string": "[user]\n"}, {"old_string": "x", "new_string": "\tname = x\n"}]),
        0,
        id="multiedit-missing-file-ordinary-fragments",
    ),
    pytest.param(
        None,
        _multi_edit([{"old_string": "", "new_string": "[user]\n"}, {"old_string": "x", "new_string": _HOOKS_PATH_SECTION}]),
        2,
        id="multiedit-missing-file-sensitive-fragments",
    ),
    # 置換で全文を上限超へ膨らませる編集は、文字列を作る前に deny する。
    pytest.param(
        " " * 100_000,
        _multi_edit([{"old_string": " ", "new_string": " " * 2_000, "replace_all": True}]),
        2,
        id="multiedit-expanding-edits-stopped-before-allocation",
    ),
    # 編集件数の上限は 64 件。
    pytest.param(_USER, _multi_edit([{"old_string": "x", "new_string": "x"}] * 65), 2, id="multiedit-65-edits"),
    pytest.param(_USER, _multi_edit([{"old_string": "x", "new_string": "x"}] * 64), 0, id="multiedit-64-edits"),
    # 編集列を読めなければ fail-closed にする。
    pytest.param(None, _multi_edit([]), 2, id="multiedit-malformed-empty"),
    pytest.param(None, _multi_edit("x"), 2, id="multiedit-malformed-not-list"),
    pytest.param(None, _multi_edit([1]), 2, id="multiedit-malformed-not-dict"),
    pytest.param(None, _multi_edit([{"old_string": "a"}]), 2, id="multiedit-malformed-no-new"),
]


@pytest.mark.parametrize(("disk", "payload", "expected"), _FILE_CASES)
def test_full_text_verdict(disk: str | bytes | None, payload: dict, expected: int, monkeypatch: pytest.MonkeyPatch) -> None:
    """置換後の全文に危険 key が新しく現れた・値が変わったときと、全文を検証できないときだけ deny する。"""
    if isinstance(disk, bytes):
        Path(".git").mkdir()
        Path(".git/config").write_bytes(disk)
    elif disk is not None:
        _put(".git/config", disk)

    assert _run(monkeypatch, payload) == expected


def test_directory_target_is_treated_as_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """読めない対象（ディレクトリ）は空として new_string を判定する。"""
    Path(".git/config").mkdir(parents=True)
    assert _run(monkeypatch, _edit("", _HOOKS_PATH_SECTION)) == 2


@pytest.mark.parametrize(
    ("payload", "snippets"),
    [
        # deny の理由は対象パスを名指しし、hooks を外す key だと説明する。
        pytest.param(_edit("", _HOOKS_PATH_SECTION), (".git/config", "hooksPath"), id="sensitive-key-names-path"),
        # 解析できないときの理由は検証不能であることを示す。
        pytest.param(_write("[user\n"), ("could not be verified",), id="unparsable-names-cause"),
    ],
)
def test_block_message(
    payload: dict, snippets: tuple[str, ...], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """deny の理由は原因に応じた文言を出す。"""
    assert _run(monkeypatch, payload) == 2
    stderr = capsys.readouterr().err
    assert all(snippet in stderr for snippet in snippets), stderr


@pytest.mark.parametrize(
    "text",
    [
        "[core]\n\thooksPath = /dev/null\n",
        "[CORE]\n HooksPath=x\n",
        "[core] hooksPath = /dev/null\n",
        "[core]\n\thooksPath\n",
        '[core]\n\thooksPath = "x y"\n',
        "[core]\n\thooksPath = x \\\n  y\n",
        "[include]\n\tpath = ../evil.gitconfig\n",
        '[includeIf "gitdir:~/x/"]\n\tpath = y\n',
        '[INCLUDEIF "gitdir:/"]\n\tpath = y\n',
        "[alias]\n\tci = commit --no-verify\n",
        "[alias]\n\tci = commit \\\n    --no-verify\n",
        "[alias]\n\tst = status\n",
        "[Alias.x]\n\ty = z\n",
    ],
    ids=[
        "hookspath",
        "case-insensitive",
        "same-line-as-header",
        "bare-key",
        "quoted-value",
        "continuation",
        "include",
        "includeif",
        "includeif-uppercase",
        "alias",
        "alias-continuation",
        "harmless-alias",
        "legacy-dotted-section",
    ],
)
def test_write_is_blocked(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """書式の違いに関わらず、Write の全内容に危険 key があれば deny する。"""
    assert _run(monkeypatch, _write(text)) == 2


@pytest.mark.parametrize(
    "text",
    [
        "# hooksPath = x\n[user]\n\tname = x\n",
        "[user]\n\tname = x ; hooksPath = y\n",
        "[user]\n\tname = x\n; [alias]\n",
        '[remote "alias"]\n\turl = u\n',
        '[remote "o"]\n\turl = https://example.com/?hookspath=1&path=2\n',
        "[aliases]\n\tci = commit\n",
        _USER + _REMOTE_SECTION,
        _REMOTE_SECTION + '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n' + _USER,
    ],
    ids=[
        "comment",
        "inline-comment",
        "commented-header",
        "alias-subsection-name",
        "value-text",
        "aliases",
        "ordinary-config",
        "ordinary-remote-branch-user",
    ],
)
def test_write_is_allowed(monkeypatch: pytest.MonkeyPatch, text: str) -> None:
    """コメント・値の中・別名の見出しにあるだけの語と、remote / branch / user の通常記述は通す。"""
    assert _run(monkeypatch, _write(text)) == 0


def test_each_sensitive_prefix_is_blocked_in_a_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """危険 key の集合は block_no_verify の `_SENSITIVE_CONFIG_KEY_PREFIXES` と 1 本で、全要素がファイル本文上でも deny される。"""
    samples = {
        "core.hookspath": "[core]\nhookspath=x",
        "include.path": "[include]\npath = x",
        "includeif.": '[includeIf "gitdir:/"]\nany = y',
        "alias.": "[alias]\nany = y",
    }
    assert set(samples) == set(block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES)
    for text in samples.values():
        assert _run(monkeypatch, _write(text)) == 2, text


class TestProtectedPaths:
    """保護対象の git config パス集合。"""

    @pytest.mark.parametrize(
        "file_path",
        [
            ".git/config",
            ".git/config.worktree",
            ".git/worktrees/wt1/config",
            ".git/worktrees/wt1/config.worktree",
            "sub/.GIT/CONFIG",
            "sub/.git/Config.Worktree",
        ],
    )
    def test_repo_paths_are_checked(self, monkeypatch: pytest.MonkeyPatch, file_path: str) -> None:
        """リポジトリ内の git config は hooksPath を持ち込む Write を deny し、通常編集は通す。"""
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=file_path)) == 2
        assert _run(monkeypatch, _write(_USER, file_path=file_path)) == 0

    @pytest.mark.parametrize(
        "file_path",
        ["config", "src/config", ".git/info/exclude", "gitconfig/config", ".gitconfig", ".git/worktrees/config", ".git/worktrees/a/b/config"],
    )
    def test_other_paths_are_not_affected(self, monkeypatch: pytest.MonkeyPatch, file_path: str) -> None:
        """git config ではないパスは hooksPath を含んでも対象外。"""
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=file_path)) == 0

    @pytest.mark.parametrize(
        ("file_path", "content", "expected"),
        [
            pytest.param("~/.gitconfig", _HOOKS_PATH_SECTION, 2, id="tilde-gitconfig-hooks-path"),
            pytest.param("{home}/.gitconfig", _HOOKS_PATH_SECTION, 2, id="absolute-gitconfig-hooks-path"),
            pytest.param("~/.gitconfig", _USER + "\temail = e@example.com\n", 0, id="tilde-gitconfig-user"),
            # XDG 未設定なら `~/.config/git/config` が対象。
            pytest.param("{home}/.config/git/config", _HOOKS_PATH_SECTION, 2, id="xdg-default-location"),
        ],
    )
    def test_global_config_write(
        self, file_path: str, content: str, expected: int, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path
    ) -> None:
        """ホーム側の git config は hooksPath を持ち込む Write を deny し、user の追加は通す（``{home}`` はホームの実パス）。"""
        payload = _write(content, file_path=file_path.format(home=_isolated_home))
        assert _run(monkeypatch, payload) == expected

    def test_global_gitconfig_edit_adding_user_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path
    ) -> None:
        """`~/.gitconfig` の既存 alias を保ったまま user を足す Edit は通し、alias の追加は deny する。"""
        _put(_isolated_home / ".gitconfig", "[alias]\n\tst = status\n")
        assert _run(monkeypatch, _edit("[alias]", "[user]\n\tname = x\n[alias]", file_path="~/.gitconfig")) == 0
        assert _run(monkeypatch, _edit("st = status\n", "st = status\n\tci = commit\n", file_path="~/.gitconfig")) == 2

    def test_xdg_config_home_is_used_when_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`XDG_CONFIG_HOME` があればその下の `git/config` が対象になる。"""
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=str(xdg / "git" / "config"))) == 2

    def test_global_config_symlink_target_is_protected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_home: Path
    ) -> None:
        """dotfiles への symlink になっている `~/.gitconfig` は、実体への書込みも同じファイルとして扱う。"""
        real = _put(tmp_path / "dotfiles" / "gitconfig", _USER)
        (_isolated_home / ".gitconfig").symlink_to(real)
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path="~/.gitconfig")) == 2
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=str(real))) == 2

    def test_unresolvable_path_is_not_global(self) -> None:
        """解決できないパスはホーム側の設定ではない。"""
        assert config_protection.global_git_config_path("bad\x00path") is None

    def test_unknown_home_means_no_global_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ホームを決められなければ、ホーム側の設定は候補なし。"""
        for name in ("CLAQ_HOME", "HOME", "USERPROFILE"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("no home"))))
        assert config_protection.global_git_config_path("~/.gitconfig") is None
        assert config_protection.global_git_config_path(".gitconfig") is None

    def test_display_names_cover_every_candidate(self) -> None:
        """表示名の集合は、リポジトリ内・ホーム側の全候補を含む。"""
        assert config_protection.repo_git_config_path(".git/worktrees/w/config") in config_protection.GIT_CONFIG_DISPLAY_NAMES
        assert config_protection.global_git_config_path("~/.gitconfig") in config_protection.GIT_CONFIG_DISPLAY_NAMES


class TestBashWrite:
    """Bash からの git config 書込みは内容に関わらず deny する。"""

    @pytest.fixture(autouse=True)
    def _repo(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """repo スコープ判定を tmp_path に固定する。"""
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            # 書込み先位置に `.git/config` があれば deny し、表示名は `.git/config`。
            pytest.param("printf '[core]\\n\\thooksPath=/dev/null\\n' >> .git/config", ".git/config", id="hookspath-append"),
            pytest.param("echo '[remote \"o\"]' >> .git/config", ".git/config", id="remote-append"),
            pytest.param("echo x > .git/config", ".git/config", id="redirect"),
            pytest.param("echo x | tee -a .git/config", ".git/config", id="tee"),
            pytest.param("sed -i 's/a/b/' .git/config", ".git/config", id="sed-i"),
            pytest.param("rm .git/config", ".git/config", id="rm"),
            pytest.param("cp /tmp/evil .git/config", ".git/config", id="cp"),
            pytest.param("chmod 000 .git/config", ".git/config", id="chmod"),
            # `config.worktree` と worktree ごとの設定も書込み先位置で deny する。
            pytest.param("echo x >> .git/config.worktree", ".git/config.worktree", id="config-worktree"),
            pytest.param("echo x > .git/worktrees/wt/config", ".git/worktrees/*/config", id="worktree-config"),
            pytest.param(
                "tee .git/worktrees/wt/config.worktree", ".git/worktrees/*/config.worktree", id="worktree-config-worktree"
            ),
            # `~/.gitconfig` と XDG の設定は、書込み先位置に現れたら内容を問わず deny する。
            pytest.param("printf x >> ~/.gitconfig", "~/.gitconfig", id="global-tilde"),
            pytest.param("echo x > $HOME/.gitconfig", "~/.gitconfig", id="global-home-var"),
            pytest.param("echo x > ${HOME}/.gitconfig", "~/.gitconfig", id="global-home-brace"),
            pytest.param("echo x | tee ~/.gitconfig", "~/.gitconfig", id="global-tee"),
            pytest.param("sed -i 's/a/b/' ~/.gitconfig", "~/.gitconfig", id="global-sed-i"),
            pytest.param("cp /tmp/evil ~/.gitconfig", "~/.gitconfig", id="global-cp"),
            pytest.param("rm ~/.gitconfig", "~/.gitconfig", id="global-rm"),
            pytest.param("echo x > ~/.config/git/config", "~/.config/git/config", id="xdg-default"),
            pytest.param("echo x > $HOME/.config/git/config", "~/.config/git/config", id="xdg-default-var"),
            # `cd` 後の相対パスは解決できないため、basename が `.gitconfig` の書込み先を deny する。
            pytest.param("cd ~ && printf x >> .gitconfig", "~/.gitconfig", id="gitconfig-after-cd"),
            pytest.param("cd ~ && printf x >> notes.txt", None, id="other-file-after-cd"),
            # 未設定の `XDG_CONFIG_HOME` は展開しない（別の位置を指す）。
            pytest.param("echo x > $XDG_CONFIG_HOME/git/config", None, id="unset-xdg-variable"),
            # 読み取り・`git config` 経由・別名は通す。
            pytest.param("cat .git/config", None, id="cat"),
            pytest.param("grep hooksPath .git/config", None, id="grep"),
            pytest.param("cp .git/config /tmp/backup", None, id="cp-source"),
            pytest.param("git config user.name x", None, id="git-config-name"),
            pytest.param("git config --get core.hooksPath", None, id="git-config-get"),
            pytest.param("echo x > .gitconfig", None, id="gitconfig"),
            pytest.param("echo x > config", None, id="bare-config"),
            pytest.param("echo '>> .git/config'", None, id="quoted"),
            pytest.param("cat ~/.gitconfig", None, id="cat-global"),
            pytest.param("grep alias ~/.gitconfig", None, id="grep-global"),
            pytest.param("cp ~/.gitconfig /tmp/backup", None, id="cp-source-global"),
            pytest.param("git config --global user.name x", None, id="git-config-global"),
            pytest.param("echo x > ~/.gitconfig.bak", None, id="backup-name"),
            pytest.param("echo x > ~/.config/git/ignore", None, id="xdg-ignore"),
            pytest.param("sed -n p ~/.gitconfig", None, id="sed-print"),
        ],
    )
    def test_find_protected_write(self, command: str, expected: str | None) -> None:
        """書込み先位置の git config だけを表示名で返し、読み取り・別名は None。"""
        assert bash_config_protection.find_protected_write(command) == expected

    def test_absolute_home_path_is_blocked(self, _isolated_home: Path) -> None:
        """ホームの絶対パスでも同じ。"""
        assert bash_config_protection.find_protected_write(f"echo x > {_isolated_home}/.gitconfig") == "~/.gitconfig"

    def test_xdg_config_home_variable_is_blocked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`XDG_CONFIG_HOME` を設定していれば `$XDG_CONFIG_HOME/git/config` が対象になる。"""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        for token in ("$XDG_CONFIG_HOME/git/config", "${XDG_CONFIG_HOME}/git/config", str(tmp_path / "xdg/git/config")):
            assert bash_config_protection.find_protected_write(f"echo x > {token}") == "$XDG_CONFIG_HOME/git/config"

    def test_global_config_is_blocked_without_repo_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """リポジトリ外の固定位置なので、リポジトリルートが決まらなくても、`cd` を挟んでも deny する。"""
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: None)
        assert bash_config_protection.find_protected_write("echo x > ~/.gitconfig") == "~/.gitconfig"
        assert bash_config_protection.find_protected_write("cd /tmp && echo x > ~/.gitconfig") == "~/.gitconfig"

    def test_global_config_symlink_target_is_blocked(self, tmp_path: Path, _isolated_home: Path) -> None:
        """dotfiles への symlink になっている `~/.gitconfig` は、実体への書込みも deny する。"""
        real = _put(tmp_path / "dotfiles" / "gitconfig", "[user]\n")
        (_isolated_home / ".gitconfig").symlink_to(real)
        assert bash_config_protection.find_protected_write(f"echo x > {real}") == "~/.gitconfig"

    @pytest.mark.parametrize(
        ("command", "display", "absent"),
        [
            # lint 設定向けでない専用文言を出す。
            pytest.param("echo x >> .git/config", ".git/config", "linter", id="repo-config"),
            # ログ保護ではなく git config 向けの文言を出す。
            pytest.param("echo x >> ~/.gitconfig", "~/.gitconfig", "prompt-injection", id="global-config"),
        ],
    )
    def test_main_message_names_git_config(
        self,
        command: str,
        display: str,
        absent: str,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """main 経路は exit 2 で、対象の表示名と git config 向けの文言を出す。"""
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
        monkeypatch.setattr(bash_config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))
        assert bash_config_protection.main() == 2
        stderr = capsys.readouterr().err
        assert (display in stderr, "git config" in stderr, absent in stderr) == (True, True, False)

    def test_git_config_hooks_path_stays_blocked_by_block_no_verify(self) -> None:
        """`git config core.hooksPath` は従来どおり block_no_verify が塞ぐ。"""
        assert block_no_verify.has_bypass_flag("git config core.hooksPath /dev/null")
        assert not block_no_verify.has_bypass_flag("git config user.name x")
