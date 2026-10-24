"""`bash_config_protection` の repo 外ガードと、解析成功経路の演算子分割のテスト。

プラグインの ``hooks/hooks.json`` とホスト設定（``.claude/settings*.json``）は repo の外でも
Bash から書かせない（`host_guard_targets`）。`shlex` が記号の連なりを 1 語にまとめる入力でも
保護対象への書き込みを見落とさない（`shell_tokens._split_operator_run`）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, host_guard_targets


def _bash_payload(command: str) -> str:
    """Bash ツール呼び出しの stdin JSON を組み立てる。"""
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})


@pytest.fixture(autouse=True)
def _fixed_repo_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """repo スコープ判定を tmp_path 配下に固定する（cwd と `resolve_repo_root` を揃える）。"""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bash_config_protection, "resolve_repo_root", lambda: tmp_path)
    return tmp_path


class TestHostGuardFilesOutsideRepo:
    """プラグインの ``hooks/hooks.json`` とホスト設定は repo の外でも Bash から書かせない。

    どちらも保護フックそのものを止められる（hooks.json は PreToolUse ガードの登録元、
    ``~/.claude/settings*.json`` は ``CLAQ_PYTHON``・``hooks``・プラグインの無効化）。
    Edit/Write（`config_protection`）は同じ書き込みを deny するので、Bash だけ repo
    スコープ判定で allow にすると片側だけ塞いだ状態になる。
    """

    @pytest.fixture(autouse=True)
    def _fixed_home(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
    ) -> Path:
        """home を repo_root（tmp_path）と兄弟の別ディレクトリに固定する。

        home が repo_root の下にあると、repo スコープ判定だけで deny され、
        repo 外を塞ぐ修正を検証したことにならない。
        """
        home = tmp_path_factory.mktemp("home")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.delenv("CLAQ_HOME", raising=False)
        return home

    @pytest.mark.parametrize(
        "command",
        [
            "printf x > ~/.claude/plugins/cache/m/claq/0/hooks/hooks.json",
            "tee ~/.claude/plugins/cache/m/claq/0/hooks/hooks.json <<< x",
            "rm ~/.claude/plugins/cache/m/claq/0/hooks/hooks.json",
            "printf x > ~/.claude/settings.json",
            "printf x >> ~/.claude/settings.local.json",
            "sed -i 's/a/b/' ~/.claude/settings.json",
            "cp weak.json ~/.claude/settings.json",
        ],
        ids=[
            "hooks-json-redirect",
            "hooks-json-tee",
            "hooks-json-rm",
            "settings-redirect",
            "settings-local-append",
            "settings-sed-i",
            "settings-cp",
        ],
    )
    def test_tilde_form_is_blocked(self, command: str) -> None:
        """``~`` 形の書込み先は repo の外でも deny する。"""
        assert bash_config_protection.find_protected_write(command) is not None

    def test_absolute_plugin_hooks_json_is_blocked(self, _fixed_home: Path) -> None:
        """`~` を介さない絶対パスでも deny する。"""
        target = _fixed_home / ".claude" / "plugins" / "cache" / "m" / "claq" / "0" / "hooks" / "hooks.json"

        assert bash_config_protection.find_protected_write(f"printf x > {target}") == "hooks/hooks.json/"

    def test_absolute_host_settings_is_blocked(self, _fixed_home: Path) -> None:
        """ホスト設定の表示名は home ログ用の文言（``~/`` 始まり）へ流れない。"""
        found = bash_config_protection.find_protected_write(f"printf x > {_fixed_home}/.claude/settings.json")

        assert found == ".claude/settings.json"
        assert "linter" in bash_config_protection._blocked_message(found)

    def test_host_settings_reached_through_symlink_is_blocked(self, _fixed_home: Path) -> None:
        """dotfiles 管理で ``~/.claude/settings.json`` が symlink でも、書く名前で塞ぐ。"""
        real = _fixed_home / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")
        (_fixed_home / ".claude").mkdir()
        (_fixed_home / ".claude" / "settings.json").symlink_to(real)

        assert bash_config_protection.find_protected_write("printf x > ~/.claude/settings.json") is not None

    def test_main_blocks_host_settings_write(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """フック本体が exit 2 と「フックを自分で無効化するな」の文言を返す。"""
        monkeypatch.setattr(
            bash_config_protection,
            "read_raw_stdin_with_truncation",
            lambda: (_bash_payload("printf x > ~/.claude/settings.json"), False),
        )

        assert bash_config_protection.main() == 2
        assert "do not disable the protection hook yourself" in capsys.readouterr().err

    def test_bare_hooks_json_after_cd_is_blocked(self) -> None:
        """`cd` で解決基準がずれるコマンドでは、basename ``hooks.json`` だけでも deny する。"""
        command = "cd ~/.claude/plugins/cache/m/claq/0/hooks && printf x > hooks.json"

        assert bash_config_protection.find_protected_write(command) == "hooks/hooks.json/"

    @pytest.mark.parametrize(
        "command",
        ["rm -rf ~/.claude/plugins/cache/m/claq/0/hooks", "rm -rf ~/.claude/plugins/cache/m/claq"],
        ids=["hooks-dir", "plugin-dir"],
    )
    def test_removing_ancestor_of_plugin_hooks_json_is_blocked(
        self, command: str, _fixed_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """プラグインの hooks.json をディレクトリごと消す形も deny する（cache 配置を模す）。"""
        monkeypatch.setattr(
            host_guard_targets,
            "get_plugin_root",
            lambda: _fixed_home / ".claude" / "plugins" / "cache" / "m" / "claq" / "0",
        )

        assert bash_config_protection.find_protected_write(command) == "hooks/hooks.json/"

    @pytest.mark.parametrize(
        "command",
        ["mv ~/.claude ~/.claude.bak", "rm -rf ~/.claude"],
        ids=["mv-config-dir", "rm-config-dir"],
    )
    def test_moving_host_config_directory_is_blocked(self, command: str) -> None:
        """ホスト設定をディレクトリごと退避・削除する形も deny する。"""
        assert bash_config_protection.find_protected_write(command) == ".claude/settings.json"

    def test_writing_real_path_of_symlinked_config_dir_is_blocked(self, _fixed_home: Path) -> None:
        """``~/.claude`` が dotfiles への symlink のとき、実体パスへ直接書く形も deny する。"""
        real = _fixed_home / "dot" / "c"
        real.mkdir(parents=True)
        (_fixed_home / ".claude").symlink_to(real)

        assert bash_config_protection.find_protected_write(f"printf x > {real}/settings.json") == ".claude/settings.json"

    def test_relocated_config_dir_is_blocked(self, _fixed_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CLAUDE_CONFIG_DIR`` で移した置き場のホスト設定も deny する。"""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_fixed_home / ".claude-work"))

        assert (
            bash_config_protection.find_protected_write("printf x > ~/.claude-work/settings.local.json")
            == ".claude/settings.local.json"
        )

    def test_guarded_locations_skip_home_when_undeterminable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ホームを決められない環境でも落ちず、プラグインの hooks.json は守り続ける。"""

        def _no_home() -> Path:
            raise OSError("no home")

        monkeypatch.setattr(host_guard_targets, "get_home_dir", _no_home)

        assert [display for _path, display in host_guard_targets._guarded_locations()] == ["hooks/hooks.json/"]

    def test_guarded_locations_skip_unresolvable_config_dir(
        self, _fixed_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """未知ユーザーの ``~user`` を指す ``CLAUDE_CONFIG_DIR`` は省き、例外で判定ごと落とさない。"""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "~claq-no-such-user-xyz/cfg")

        displays = [display for _path, display in host_guard_targets._guarded_locations()]

        assert displays == ["hooks/hooks.json/", ".claude/settings.json", ".claude/settings.local.json"]

    @pytest.mark.parametrize(
        "command",
        [
            # リポジトリ外の `.git/hooks` は従来どおり通す（repo スコープ判定の対象）。
            "printf x > /definitely-outside-the-repo/.git/hooks/pre-commit",
            # consumer 側の `.claude/hooks.json` は `/harness --apply` の正当な書き込み先。
            "printf x > ~/.claude/hooks.json",
            "printf x > ~/.claude/other.json",
            "printf x > ~/notes/settings.json",
            # 保護位置の祖先ではないディレクトリへの複製。
            "cp notes.txt ~/Downloads/",
        ],
        ids=["outside-git-hooks", "consumer-hooks-json", "other-claude-file", "settings-elsewhere", "cp-to-other-dir"],
    )
    def test_lookalikes_outside_repo_are_allowed(self, command: str) -> None:
        """保護位置に当たらない repo 外の書込みは allow のまま。"""
        assert bash_config_protection.find_protected_write(command) is None


class TestGluedOperatorRunsOnParsedPath:
    """`shlex` が記号の連なりを 1 語にしても、保護対象への書き込みを見落とさない。"""

    @pytest.mark.parametrize(
        "command",
        [
            "true;(rm ruff.toml)",
            "(printf x)>.git/hooks/pre-commit",
            "echo x|&tee ruff.toml",
            # `<>` は読み書きオープンで、無ければ作成する（空ファイル化と同じ効果）。
            "printf x <>ruff.toml",
            "printf x 1<> ruff.toml",
        ],
        ids=["semicolon-paren", "paren-redirect", "pipe-amp", "read-write-open", "fd-read-write-open"],
    )
    def test_glued_operator_run_is_detected(self, command: str) -> None:
        """記号の連なりに埋もれた区切り・リダイレクトを読み、保護対象への書込みを deny する。"""
        assert bash_config_protection.find_protected_write(command) is not None

    @pytest.mark.parametrize(
        "command",
        ["echo x >&2", "cmd 2>&1;(echo ruff.toml)", "cat <<<ruff.toml"],
        ids=["fd-dup", "fd-dup-then-subshell", "herestring"],
    )
    def test_non_write_operator_runs_stay_allowed(self, command: str) -> None:
        """fd 複製・here-string は書込みと読まない。"""
        assert bash_config_protection.find_protected_write(command) is None
