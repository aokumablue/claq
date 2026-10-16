"""`.git/config` 経由のフック迂回（hooksPath 等）に対する保護のテスト。

Edit/Write は「フック実行を左右する config key の兆候が内容に含まれるときだけ」deny し、
remote / branch の通常編集は通す。Bash からの書込みは内容を解釈せず deny する。
兆候は `block_no_verify` が `git config` 経由で塞ぐ key 集合から導く。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, block_no_verify, config_protection

_REMOTE_SECTION = '[remote "origin"]\n\turl = git@example.com:o/r.git\n\tfetch = +refs/heads/*:refs/remotes/origin/*\n'
_HOOKS_PATH_SECTION = "[core]\n\thooksPath = /dev/null\n"


def _run(monkeypatch: pytest.MonkeyPatch, payload: dict) -> int:
    """config_protection.main() を実行し終了コードを返す。"""
    monkeypatch.setattr(config_protection, "read_raw_stdin_with_truncation", lambda: (json.dumps(payload), False))
    return config_protection.main()


def _edit(new_string: str, *, old_string: str = "", file_path: str = ".git/config") -> dict:
    """Edit ツールの payload を作る。"""
    return {
        "tool_name": "Edit",
        "tool_input": {"file_path": file_path, "old_string": old_string, "new_string": new_string},
    }


class TestEditWriteSignal:
    """Edit/Write は兆候付きの内容だけ deny する。"""

    def test_edit_adding_remote_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote を足す Edit は通す。"""
        assert _run(monkeypatch, _edit(_REMOTE_SECTION)) == 0

    def test_edit_adding_hooks_path_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """hooksPath を足す Edit は deny する。"""
        assert _run(monkeypatch, _edit(_HOOKS_PATH_SECTION)) == 2

    def test_block_message_names_the_path(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """deny の理由は `.git/config` を名指しし、通常設定は git config を使うよう促す。"""
        assert _run(monkeypatch, _edit(_HOOKS_PATH_SECTION)) == 2
        stderr = capsys.readouterr().err
        assert ".git/config" in stderr
        assert "git config" in stderr

    @pytest.mark.parametrize(
        "text",
        [
            "[core]\n\thooksPath = /dev/null\n",
            "[CORE]\n\tHOOKSPATH=/dev/null\n",
            "[core] hooksPath = /dev/null\n",
            "\thooksPath = /dev/null",
            "[include]\n\tpath = ../evil.gitconfig\n",
            '[includeIf "gitdir:/"]\n\tpath = ../evil.gitconfig\n',
            "[alias]\n\tci = commit --no-verify\n",
        ],
        ids=["hookspath", "case-insensitive", "same-line-as-header", "key-only-edit", "include", "includeif", "alias"],
    )
    def test_every_sensitive_key_family_is_blocked(self, monkeypatch: pytest.MonkeyPatch, text: str) -> None:
        """block_no_verify が塞ぐ key 系統は、ファイル本文上の綴りでも deny する。"""
        assert _run(monkeypatch, _edit(text)) == 2

    def test_write_with_hooks_path_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Write の全内容に hooksPath があれば deny する。"""
        payload = {"tool_name": "Write", "tool_input": {"file_path": ".git/config", "content": _HOOKS_PATH_SECTION}}
        assert _run(monkeypatch, payload) == 2

    def test_write_of_ordinary_config_is_allowed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """兆候の無い Write は通す。"""
        payload = {
            "tool_name": "Write",
            "tool_input": {"file_path": ".git/config", "content": "[user]\n\tname = x\n" + _REMOTE_SECTION},
        }
        assert _run(monkeypatch, payload) == 0

    def test_multiedit_with_hooks_path_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """MultiEdit は断片のどれかに兆候があれば deny する。"""
        payload = {
            "tool_name": "MultiEdit",
            "tool_input": {
                "file_path": ".git/config",
                "edits": [
                    {"old_string": "a", "new_string": "b"},
                    {"old_string": "c", "new_string": _HOOKS_PATH_SECTION},
                ],
            },
        }
        assert _run(monkeypatch, payload) == 2

    def test_unverifiable_content_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容が取れない書込みは他の条件付き保護対象と同じく fail-closed にする。"""
        payload = {"tool_name": "Write", "tool_input": {"file_path": ".git/config"}}
        assert _run(monkeypatch, payload) == 2

    def test_over_cap_text_is_blocked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """走査上限を超える内容は走査せず deny する。"""
        big = "# pad\n" * (config_protection._LINT_SIGNAL_MAX_TEXT_BYTES // 6 + 10)
        assert _run(monkeypatch, _edit(big)) == 2

    def test_nested_git_dir_is_matched_case_insensitively(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """サブディレクトリ配下の `.GIT/CONFIG` も同じ判定になる。"""
        payload = _edit(_HOOKS_PATH_SECTION, file_path="sub/.GIT/CONFIG")
        assert _run(monkeypatch, payload) == 2

    @pytest.mark.parametrize("file_path", ["config", "src/config", ".git/info/exclude", "gitconfig/config"])
    def test_other_paths_are_not_affected(self, monkeypatch: pytest.MonkeyPatch, file_path: str) -> None:
        """`.git/config` 以外の同名ファイルは hooksPath を含んでも対象外。"""
        assert _run(monkeypatch, _edit(_HOOKS_PATH_SECTION, file_path=file_path)) == 0


class TestSignalDerivation:
    """兆候の正規表現は block_no_verify の key 定数から導く。"""

    def test_each_sensitive_prefix_is_covered(self) -> None:
        """`_SENSITIVE_CONFIG_KEY_PREFIXES` の全要素が少なくとも 1 つの書式で検出される。"""
        samples = {
            "core.hookspath": "[core]\nhookspath=x",
            "include.path": "[include]\npath = x",
            "includeif.": '[includeIf "gitdir:/"]',
            "alias.": "[alias]",
        }
        assert set(samples) == set(block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES)
        for text in samples.values():
            assert block_no_verify.GIT_CONFIG_TEXT_SIGNAL_RE.search(text), text

    def test_ordinary_sections_do_not_match(self) -> None:
        """remote / branch / user の通常記述は一致しない。"""
        text = _REMOTE_SECTION + '[branch "main"]\n\tremote = origin\n\tmerge = refs/heads/main\n[user]\n\tname = x\n'
        assert block_no_verify.GIT_CONFIG_TEXT_SIGNAL_RE.search(text) is None

    def test_value_containing_key_name_does_not_match(self) -> None:
        """値の途中に key 名が現れるだけの行は一致しない。"""
        text = "[remote \"o\"]\n\turl = https://example.com/?hookspath=1&path=2\n"
        assert block_no_verify.GIT_CONFIG_TEXT_SIGNAL_RE.search(text) is None


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
        ],
        ids=["cat", "grep", "cp-source", "git-config-name", "git-config-get", "gitconfig", "bare-config", "quoted"],
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

    def test_git_config_hooks_path_stays_blocked_by_block_no_verify(self) -> None:
        """`git config core.hooksPath` は従来どおり block_no_verify が塞ぐ。"""
        assert block_no_verify.has_bypass_flag("git config core.hooksPath /dev/null")
        assert not block_no_verify.has_bypass_flag("git config user.name x")
