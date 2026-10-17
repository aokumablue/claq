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


def _multi_edit(edits: list[dict], *, file_path: str = ".git/config") -> dict:
    """MultiEdit ツールの payload を作る。"""
    return {"tool_name": "MultiEdit", "tool_input": {"file_path": file_path, "edits": edits}}


def _put(path: str | Path, text: str) -> Path:
    """親ディレクトリごと作ってファイルを置く。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


class TestEditAgainstDisk:
    """Edit はディスク上の全文へ置換を当てて判定する。"""

    def test_fragment_into_existing_alias_section_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既存 `[alias]` の内側へ見出し無しで足す危険な alias を検出する。"""
        _put(".git/config", _ALIAS_CONFIG)
        payload = _edit("\tci = commit\n", "\tci = commit\n\tpush = push --no-verify\n")
        assert _run(monkeypatch, payload) == 2

    def test_edit_adding_remote_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote を足す Edit は通す（既存 alias が前後で同一）。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit(_REMOTE_SECTION, _REMOTE_SECTION + '[remote "up"]\n\turl = u\n')) == 0

    def test_edit_changing_remote_url_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote の url を書き換える Edit は通す。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("git@example.com:o/r.git", "git@example.com:o/new.git")) == 0

    def test_edit_adding_branch_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """branch を足す Edit は通す。"""
        _put(".git/config", _ALIAS_CONFIG)
        new = '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n'
        assert _run(monkeypatch, _edit("\tbare = false\n", "\tbare = false\n" + new)) == 0

    def test_edit_changing_alias_value_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既存 alias の値を危険側へ書き換える Edit は deny。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("ci = commit\n", "ci = commit --no-verify\n")) == 2

    def test_edit_adding_hooks_path_into_existing_core_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既存 `[core]` の内側へ hooksPath だけを足す Edit は deny。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("\tbare = false\n", "\tbare = false\n\thooksPath = /dev/null\n")) == 2

    def test_header_rename_into_alias_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """見出しだけを `[alias]` に直して既存 key を alias 扱いにする Edit は deny。"""
        _put(".git/config", "[aliases]\n\tci = commit --no-verify\n")
        assert _run(monkeypatch, _edit("[aliases]", "[alias]")) == 2

    def test_aliases_heading_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`[alias]` ではない `[aliases]` 見出しは通す。"""
        _put(".git/config", "[user]\n\tname = x\n")
        assert _run(monkeypatch, _edit("[user]", "[aliases]\n\tx = y\n[user]")) == 0

    def test_path_key_in_non_include_section_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`path =` は include 以外のセクションなら通す。"""
        _put(".git/config", '[submodule "lib"]\n\turl = u\n')
        assert _run(monkeypatch, _edit("url = u\n", "url = u\n\tpath = vendor/lib\n")) == 0

    def test_removing_shadowing_alias_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """後ろの上書きだけを消して危険な値を実効にする Edit は deny。"""
        _put(".git/config", "[alias]\n\tci = commit --no-verify\n\tci = commit\n")
        assert _run(monkeypatch, _edit("\tci = commit\n", "")) == 2

    def test_removing_alias_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """alias を消すだけの Edit は通す。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("[alias]\n\tci = commit\n", "")) == 0

    def test_replace_all_applies_every_occurrence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`replace_all` は全出現へ置換を当てて判定する。"""
        _put(".git/config", "[alias]\n\ta = x\n\tb = x\n")
        assert _run(monkeypatch, _edit("= x", "= x --no-verify", replace_all=True)) == 2
        assert _run(monkeypatch, _edit("a = x", "a = x --no-verify")) == 2

    def test_multiple_occurrences_replace_first_only(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`replace_all` 無しは最初の 1 件だけを置換する。"""
        _put(".git/config", "[user]\n\tname = x\n[remote \"o\"]\n\turl = x\n")
        assert _run(monkeypatch, _edit("= x", "= y")) == 0

    def test_old_string_not_found_in_existing_file_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容のあるファイルで置換対象が見つからなければ、実ツールとの食い違いを排除できないため deny する。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("no such text", _REMOTE_SECTION)) == 2

    def test_empty_old_string_on_existing_file_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """空の old_string は全文字の間への挿入にせず、内容のあるファイルでは deny する。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _edit("", _REMOTE_SECTION)) == 2

    def test_lone_carriage_return_is_not_converted_when_reading(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """単独の CR を改行に変換せずに読む。変換すると、コメントの一部だった行を危険 key の既存行と誤認する。"""
        Path(".git").mkdir()
        Path(".git/config").write_bytes(b"[user]\n# x\r[core] hooksPath = /e\n")
        assert _run(monkeypatch, _edit("# x", "")) == 2

    def test_crlf_file_keeps_unrelated_edits_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CRLF のファイルでも、無関係な編集は通す。"""
        Path(".git").mkdir()
        Path(".git/config").write_bytes(b"[alias]\r\n\tci = commit\r\n[user]\r\n\tname = x\r\n")
        assert _run(monkeypatch, _edit("name = x", "name = y")) == 0

    def test_missing_or_empty_file_uses_new_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ファイルが無い・空なら new_string が全文になる。"""
        _put(".git/config", "")
        assert _run(monkeypatch, _edit("", _REMOTE_SECTION)) == 0
        Path(".git/config").unlink()
        assert _run(monkeypatch, _edit("", _REMOTE_SECTION)) == 0
        assert _run(monkeypatch, _edit("x", _HOOKS_PATH_SECTION)) == 2

    def test_directory_target_is_treated_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """読めない対象（ディレクトリ）は空として new_string を判定する。"""
        Path(".git/config").mkdir(parents=True)
        assert _run(monkeypatch, _edit("", _HOOKS_PATH_SECTION)) == 2

    def test_block_message_names_the_path(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """deny の理由は対象パスを名指しし、hooks を外す key だと説明する。"""
        assert _run(monkeypatch, _edit("", _HOOKS_PATH_SECTION)) == 2
        stderr = capsys.readouterr().err
        assert ".git/config" in stderr
        assert "hooksPath" in stderr


class TestSensitiveSyntax:
    """書式の違いに関わらず、危険 key を deny する。"""

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
    def test_write_is_blocked(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        """Write の全内容に危険 key があれば deny する。"""
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
        ],
        ids=["comment", "inline-comment", "commented-header", "alias-subsection-name", "value-text", "aliases"],
    )
    def test_write_is_allowed(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        """コメント・値の中・別名の見出しにあるだけの語は通す。"""
        assert _run(monkeypatch, _write(text)) == 0

    def test_write_of_ordinary_config_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """危険 key の無い Write は通す。"""
        assert _run(monkeypatch, _write("[user]\n\tname = x\n" + _REMOTE_SECTION)) == 0

    def test_rewriting_identical_alias_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """既存と同一の危険 key を書き直す Write は通す。"""
        _put(".git/config", _ALIAS_CONFIG)
        assert _run(monkeypatch, _write(_ALIAS_CONFIG + '[branch "m"]\n\tremote = o\n')) == 0

    def test_unparsable_result_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """解析できない全文は deny する。"""
        assert _run(monkeypatch, _write("[user\n")) == 2
        assert _run(monkeypatch, _edit("", "\thooksPath = /dev/null")) == 2

    def test_unresolvable_target_is_treated_as_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """解決できないパス（NUL 混入）でも、new_string を全文として判定する。"""
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path="\x00/.git/config")) == 2
        assert _run(monkeypatch, _write("[user]\n\tname = x\n", file_path="\x00/.git/config")) == 0

    def test_unparsable_message_names_the_cause(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """解析できないときの理由は検証不能であることを示す。"""
        assert _run(monkeypatch, _write("[user\n")) == 2
        assert "could not be verified" in capsys.readouterr().err

    def test_unparsable_existing_file_does_not_block_ordinary_edit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """置換前が解析できなくても、置換後が解析できて危険 key が無ければ通す。"""
        _put(".git/config", "garbage\n")
        assert _run(monkeypatch, _write("[user]\n\tname = x\n")) == 0

    def test_unverifiable_content_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容が取れない書込みは他の条件付き保護対象と同じく fail-closed にする。"""
        assert _run(monkeypatch, {"tool_name": "Write", "tool_input": {"file_path": ".git/config"}}) == 2

    def test_apply_patch_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """パッチ本文からは全文を作れないため deny する。"""
        payload = {"tool_name": "apply_patch", "tool_input": {"input": "*** Update File: .git/config\n+[user]\n"}}
        assert _run(monkeypatch, payload) == 2

    def test_over_cap_result_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """走査上限を超える全文は走査せず deny する。"""
        big = "# pad\n" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES // 6 + 10)
        assert _run(monkeypatch, _write(big)) == 2

    def test_over_cap_existing_file_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """置換前が走査上限を超える場合も、上限より後ろへの追記を見逃さず deny する。"""
        big = "# pad\n" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES // 6 + 10)
        _put(".git/config", big + "[user]\n\tname = x\n")
        assert _run(monkeypatch, _edit("name = x", "name = y")) == 2


class TestMultiEdit:
    """MultiEdit は編集を順に当てた全文で判定する。"""

    def test_sequential_edits_compose(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """後の編集は前の編集の結果へ当たる。"""
        _put(".git/config", "[user]\n\tname = x\n")
        edits = [
            {"old_string": "[user]", "new_string": "[alias]"},
            {"old_string": "name = x", "new_string": "name = commit --no-verify"},
        ]
        assert _run(monkeypatch, _multi_edit(edits)) == 2

    def test_ordinary_edits_are_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """危険 key の無い編集列は通す。"""
        _put(".git/config", _ALIAS_CONFIG)
        edits = [
            {"old_string": "bare = false", "new_string": "bare = true"},
            {"old_string": "r.git", "new_string": "r2.git"},
        ]
        assert _run(monkeypatch, _multi_edit(edits)) == 0

    def test_replace_all_per_edit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """edit ごとの `replace_all` を尊重する。"""
        _put(".git/config", "[alias]\n\ta = x\n\tb = x\n")
        edits = [{"old_string": "= x", "new_string": "= y", "replace_all": True}]
        assert _run(monkeypatch, _multi_edit(edits)) == 2

    def test_unapplicable_edit_on_existing_file_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容のあるファイルで適用できない編集が 1 件でもあれば deny する。"""
        _put(".git/config", _ALIAS_CONFIG)
        edits = [
            {"old_string": "bare = false", "new_string": "bare = true"},
            {"old_string": "missing", "new_string": "[user]\n\tname = x\n"},
        ]
        assert _run(monkeypatch, _multi_edit(edits)) == 2

    def test_edits_on_missing_file_are_collected_as_fragments(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ファイルが無いときは全編集の new_string を連結した全文で判定する。"""
        ordinary = [{"old_string": "", "new_string": "[user]\n"}, {"old_string": "x", "new_string": "\tname = x\n"}]
        assert _run(monkeypatch, _multi_edit(ordinary)) == 0
        sensitive = [{"old_string": "", "new_string": "[user]\n"}, {"old_string": "x", "new_string": _HOOKS_PATH_SECTION}]
        assert _run(monkeypatch, _multi_edit(sensitive)) == 2

    def test_expanding_edits_are_stopped_before_allocation(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """置換で全文を上限超へ膨らませる編集は、文字列を作る前に deny する。"""
        _put(".git/config", " " * 100_000)
        edits = [{"old_string": " ", "new_string": " " * 2_000, "replace_all": True}]
        assert _run(monkeypatch, _multi_edit(edits)) == 2

    def test_too_many_edits_are_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """編集件数が上限を超える MultiEdit は deny する。"""
        _put(".git/config", "[user]\n\tname = x\n")
        edits = [{"old_string": "x", "new_string": "x"}] * 65
        assert _run(monkeypatch, _multi_edit(edits)) == 2
        assert _run(monkeypatch, _multi_edit(edits[:64])) == 0

    @pytest.mark.parametrize("edits", [[], "x", [1], [{"old_string": "a"}]], ids=["empty", "not-list", "not-dict", "no-new"])
    def test_malformed_edits_are_blocked(self, monkeypatch: pytest.MonkeyPatch, edits: object) -> None:
        """編集列を読めなければ fail-closed にする。"""
        assert _run(monkeypatch, _multi_edit(edits)) == 2  # type: ignore[arg-type]


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
        assert _run(monkeypatch, _write("[user]\n\tname = x\n", file_path=file_path)) == 0

    @pytest.mark.parametrize(
        "file_path",
        ["config", "src/config", ".git/info/exclude", "gitconfig/config", ".gitconfig", ".git/worktrees/config", ".git/worktrees/a/b/config"],
    )
    def test_other_paths_are_not_affected(self, monkeypatch: pytest.MonkeyPatch, file_path: str) -> None:
        """git config ではないパスは hooksPath を含んでも対象外。"""
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=file_path)) == 0

    def test_global_gitconfig_is_checked(self, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path) -> None:
        """`~/.gitconfig` は hooksPath を持ち込む Write を deny し、user の追加は通す。"""
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path="~/.gitconfig")) == 2
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=str(_isolated_home / ".gitconfig"))) == 2
        assert _run(monkeypatch, _write("[user]\n\tname = x\n\temail = e@example.com\n", file_path="~/.gitconfig")) == 0

    def test_global_gitconfig_edit_adding_user_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path
    ) -> None:
        """`~/.gitconfig` の既存 alias を保ったまま user を足す Edit は通し、alias の追加は deny する。"""
        _put(_isolated_home / ".gitconfig", "[alias]\n\tst = status\n")
        assert _run(monkeypatch, _edit("[alias]", "[user]\n\tname = x\n[alias]", file_path="~/.gitconfig")) == 0
        assert _run(monkeypatch, _edit("st = status\n", "st = status\n\tci = commit\n", file_path="~/.gitconfig")) == 2

    def test_xdg_config_default_location(self, monkeypatch: pytest.MonkeyPatch, _isolated_home: Path) -> None:
        """XDG 未設定なら `~/.config/git/config` が対象。"""
        path = str(_isolated_home / ".config" / "git" / "config")
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=path)) == 2

    def test_xdg_config_home_is_used_when_set(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`XDG_CONFIG_HOME` があればその下の `git/config` が対象になる。"""
        xdg = tmp_path / "xdg"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
        assert _run(monkeypatch, _write(_HOOKS_PATH_SECTION, file_path=str(xdg / "git" / "config"))) == 2

    def test_global_config_symlink_target_is_protected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _isolated_home: Path
    ) -> None:
        """dotfiles への symlink になっている `~/.gitconfig` は、実体への書込みも同じファイルとして扱う。"""
        real = _put(tmp_path / "dotfiles" / "gitconfig", "[user]\n\tname = x\n")
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


class TestSensitiveKeyDerivation:
    """危険 key の集合は block_no_verify の定数と 1 本である。"""

    def test_each_sensitive_prefix_is_blocked_in_a_file(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`_SENSITIVE_CONFIG_KEY_PREFIXES` の全要素が、ファイル本文上の書式でも deny される。"""
        samples = {
            "core.hookspath": "[core]\nhookspath=x",
            "include.path": "[include]\npath = x",
            "includeif.": '[includeIf "gitdir:/"]\nany = y',
            "alias.": "[alias]\nany = y",
        }
        assert set(samples) == set(block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES)
        for text in samples.values():
            assert _run(monkeypatch, _write(text)) == 2, text

    def test_ordinary_sections_are_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote / branch / user の通常記述は通す。"""
        text = _REMOTE_SECTION + '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n[user]\n\tname = x\n'
        assert _run(monkeypatch, _write(text)) == 0


class TestBashWrite:
    """Bash からの `.git/config` 書込みは内容に関わらず deny する。"""

    @pytest.fixture(autouse=True)
    def _repo(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """repo スコープ判定を tmp_path に固定する。"""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)

    @pytest.mark.parametrize(
        "command",
        [
            "printf '[core]\\n\\thooksPath=/dev/null\\n' >> .git/config",
            "echo '[remote \"o\"]' >> .git/config",
            "echo x > .git/config",
            "echo x | tee -a .git/config",
            "sed -i 's/a/b/' .git/config",
            "rm .git/config",
            "cp /tmp/evil .git/config",
            "chmod 000 .git/config",
        ],
        ids=["hookspath-append", "remote-append", "redirect", "tee", "sed-i", "rm", "cp", "chmod"],
    )
    def test_writes_are_blocked(self, command: str) -> None:
        """書込み先位置に `.git/config` があれば deny し、表示名は `.git/config`。"""
        assert bash_config_protection.find_protected_write(command) == ".git/config"

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("echo x >> .git/config.worktree", ".git/config.worktree"),
            ("echo x > .git/worktrees/wt/config", ".git/worktrees/*/config"),
            ("tee .git/worktrees/wt/config.worktree", ".git/worktrees/*/config.worktree"),
        ],
        ids=["config-worktree", "worktree-config", "worktree-config-worktree"],
    )
    def test_other_repo_config_files_are_blocked(self, command: str, expected: str) -> None:
        """`config.worktree` と worktree ごとの設定も書込み先位置で deny する。"""
        assert bash_config_protection.find_protected_write(command) == expected

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            ("printf x >> ~/.gitconfig", "~/.gitconfig"),
            ("echo x > $HOME/.gitconfig", "~/.gitconfig"),
            ("echo x > ${HOME}/.gitconfig", "~/.gitconfig"),
            ("echo x | tee ~/.gitconfig", "~/.gitconfig"),
            ("sed -i 's/a/b/' ~/.gitconfig", "~/.gitconfig"),
            ("cp /tmp/evil ~/.gitconfig", "~/.gitconfig"),
            ("rm ~/.gitconfig", "~/.gitconfig"),
            ("echo x > ~/.config/git/config", "~/.config/git/config"),
            ("echo x > $HOME/.config/git/config", "~/.config/git/config"),
        ],
        ids=["tilde", "home-var", "home-brace", "tee", "sed-i", "cp", "rm", "xdg-default", "xdg-default-var"],
    )
    def test_global_config_writes_are_blocked(self, command: str, expected: str) -> None:
        """`~/.gitconfig` と XDG の設定は、書込み先位置に現れたら内容を問わず deny する。"""
        assert bash_config_protection.find_protected_write(command) == expected

    def test_absolute_home_path_is_blocked(self, _isolated_home: Path) -> None:
        """ホームの絶対パスでも同じ。"""
        assert bash_config_protection.find_protected_write(f"echo x > {_isolated_home}/.gitconfig") == "~/.gitconfig"

    def test_xdg_config_home_variable_is_blocked(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """`XDG_CONFIG_HOME` を設定していれば `$XDG_CONFIG_HOME/git/config` が対象になる。"""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        for token in ("$XDG_CONFIG_HOME/git/config", "${XDG_CONFIG_HOME}/git/config", str(tmp_path / "xdg/git/config")):
            assert bash_config_protection.find_protected_write(f"echo x > {token}") == "$XDG_CONFIG_HOME/git/config"

    def test_unset_xdg_variable_is_not_expanded(self) -> None:
        """未設定の `XDG_CONFIG_HOME` は展開しない（別の位置を指す）。"""
        assert bash_config_protection.find_protected_write("echo x > $XDG_CONFIG_HOME/git/config") is None

    def test_global_config_is_blocked_without_repo_root(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """リポジトリ外の固定位置なので、リポジトリルートが決まらなくても、`cd` を挟んでも deny する。"""
        monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: None)
        assert bash_config_protection.find_protected_write("echo x > ~/.gitconfig") == "~/.gitconfig"
        assert bash_config_protection.find_protected_write("cd /tmp && echo x > ~/.gitconfig") == "~/.gitconfig"

    def test_gitconfig_after_cd_is_blocked(self) -> None:
        """`cd` 後の相対パスは解決できないため、basename が `.gitconfig` の書込み先を deny する。"""
        assert bash_config_protection.find_protected_write("cd ~ && printf x >> .gitconfig") == "~/.gitconfig"
        assert bash_config_protection.find_protected_write("cd ~ && printf x >> notes.txt") is None

    def test_global_config_symlink_target_is_blocked(self, tmp_path: Path, _isolated_home: Path) -> None:
        """dotfiles への symlink になっている `~/.gitconfig` は、実体への書込みも deny する。"""
        real = _put(tmp_path / "dotfiles" / "gitconfig", "[user]\n")
        (_isolated_home / ".gitconfig").symlink_to(real)
        assert bash_config_protection.find_protected_write(f"echo x > {real}") == "~/.gitconfig"

    @pytest.mark.parametrize(
        "command",
        [
            "cat .git/config",
            "grep hooksPath .git/config",
            "cp .git/config /tmp/backup",
            "git config user.name x",
            "git config --get core.hooksPath",
            "echo x > .gitconfig",
            "echo x > config",
            "echo '>> .git/config'",
            "cat ~/.gitconfig",
            "grep alias ~/.gitconfig",
            "cp ~/.gitconfig /tmp/backup",
            "git config --global user.name x",
            "echo x > ~/.gitconfig.bak",
            "echo x > ~/.config/git/ignore",
            "sed -n p ~/.gitconfig",
        ],
        ids=[
            "cat",
            "grep",
            "cp-source",
            "git-config-name",
            "git-config-get",
            "gitconfig",
            "bare-config",
            "quoted",
            "cat-global",
            "grep-global",
            "cp-source-global",
            "git-config-global",
            "backup-name",
            "xdg-ignore",
            "sed-print",
        ],
    )
    def test_reads_and_git_config_are_allowed(self, command: str) -> None:
        """読み取り・`git config` 経由・別名は通す。"""
        assert bash_config_protection.find_protected_write(command) is None

    def test_main_message_names_git_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """main 経路は exit 2 で、lint 設定向けでない専用文言を出す。"""
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo x >> .git/config"}})
        monkeypatch.setattr(bash_config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))
        assert bash_config_protection.main() == 2
        stderr = capsys.readouterr().err
        assert ".git/config" in stderr
        assert "git config" in stderr
        assert "linter" not in stderr

    def test_main_message_names_global_config(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`~/.gitconfig` への書込みは、ログ保護ではなく git config 向けの文言を出す。"""
        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo x >> ~/.gitconfig"}})
        monkeypatch.setattr(bash_config_protection, "read_raw_stdin_with_truncation", lambda: (payload, False))
        assert bash_config_protection.main() == 2
        stderr = capsys.readouterr().err
        assert "~/.gitconfig" in stderr
        assert "git config" in stderr
        assert "prompt-injection" not in stderr

    def test_git_config_hooks_path_stays_blocked_by_block_no_verify(self) -> None:
        """`git config core.hooksPath` は従来どおり block_no_verify が塞ぐ。"""
        assert block_no_verify.has_bypass_flag("git config core.hooksPath /dev/null")
        assert not block_no_verify.has_bypass_flag("git config user.name x")
