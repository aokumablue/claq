"""`bash_config_protection` の repo 外ガードと、解析成功経路の演算子分割のテスト。

プラグインの ``hooks/hooks.json`` とホスト設定（``.claude/settings*.json``）は repo の外でも
Bash から書かせない（`host_guard_targets`）。`shlex` が記号の連なりを 1 語にまとめる入力でも
保護対象への書き込みを見落とさない（`shell_tokens._split_operator_run`）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from claq.hooks import bash_config_protection, host_guard_targets

from ._support import _EXPECTED_LEADING_SHELL_KEYWORDS, _bash, _run
from ._support import _home_outside_repo as _home_outside_repo
from ._support import _repo_scope as _repo_scope


class TestHostGuardFilesOutsideRepo:
    """プラグインの ``hooks/hooks.json`` とホスト設定は repo の外でも Bash から書かせない。

    どちらも保護フックそのものを止められる（hooks.json は PreToolUse ガードの登録元、
    ``~/.claude/settings*.json`` は ``CLAQ_PYTHON``・``hooks``・プラグインの無効化）。
    Edit/Write（`config_protection`）は同じ書き込みを deny するので、Bash だけ repo
    スコープ判定で allow にすると片側だけ塞いだ状態になる。home は repo ルートと兄弟の
    ディレクトリに固定する（`_home_outside_repo`）。
    """

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

    @pytest.mark.parametrize(
        ("command", "expected"),
        [
            # `~` を介さない絶対パス。
            ("printf x > {home}/.claude/plugins/cache/m/claq/0/hooks/hooks.json", "hooks/hooks.json/"),
            ("printf x > {home}/.claude/settings.json", ".claude/settings.json"),
            # `cd` で解決基準がずれるコマンドでは、basename ``hooks.json`` だけでも deny する。
            ("cd ~/.claude/plugins/cache/m/claq/0/hooks && printf x > hooks.json", "hooks/hooks.json/"),
            # ホスト設定をディレクトリごと退避・削除する形。
            ("mv ~/.claude ~/.claude.bak", ".claude/settings.json"),
            ("rm -rf ~/.claude", ".claude/settings.json"),
        ],
        ids=["absolute-hooks-json", "absolute-settings", "bare-hooks-json-after-cd", "mv-config-dir", "rm-config-dir"],
    )
    def test_guarded_target_is_named(self, command: str, expected: str, _home_outside_repo: Path) -> None:
        """書込み先の形によらず、保護位置の表示名で deny する。"""
        assert bash_config_protection.find_protected_write(command.format(home=_home_outside_repo)) == expected

    def test_host_settings_reached_through_symlink_is_blocked(self, _home_outside_repo: Path) -> None:
        """dotfiles 管理で ``~/.claude/settings.json`` が symlink でも、書く名前で塞ぐ。"""
        real = _home_outside_repo / "dotfiles" / "claude-settings.json"
        real.parent.mkdir()
        real.write_text("{}", encoding="utf-8")
        (_home_outside_repo / ".claude").mkdir()
        (_home_outside_repo / ".claude" / "settings.json").symlink_to(real)

        assert bash_config_protection.find_protected_write("printf x > ~/.claude/settings.json") is not None

    def test_main_blocks_host_settings_write(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """exit 2 と「フックを自分で無効化するな」の文言を返し、home ログ用の文言（``~/`` 始まり）へ流れない。"""
        assert _run("bash_config_protection", _bash("printf x > ~/.claude/settings.json"), monkeypatch) == 2
        err = capsys.readouterr().err
        assert "do not disable the protection hook yourself" in err
        assert "linter" in err

    @pytest.mark.parametrize(
        "command",
        ["rm -rf ~/.claude/plugins/cache/m/claq/0/hooks", "rm -rf ~/.claude/plugins/cache/m/claq"],
        ids=["hooks-dir", "plugin-dir"],
    )
    def test_removing_ancestor_of_plugin_hooks_json_is_blocked(
        self, command: str, _home_outside_repo: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """プラグインの hooks.json をディレクトリごと消す形も deny する（cache 配置を模す）。"""
        monkeypatch.setattr(
            host_guard_targets,
            "get_plugin_root",
            lambda: _home_outside_repo / ".claude" / "plugins" / "cache" / "m" / "claq" / "0",
        )

        assert bash_config_protection.find_protected_write(command) == "hooks/hooks.json/"

    def test_writing_real_path_of_symlinked_config_dir_is_blocked(self, _home_outside_repo: Path) -> None:
        """``~/.claude`` が dotfiles への symlink のとき、実体パスへ直接書く形も deny する。"""
        real = _home_outside_repo / "dot" / "c"
        real.mkdir(parents=True)
        (_home_outside_repo / ".claude").symlink_to(real)

        assert bash_config_protection.find_protected_write(f"printf x > {real}/settings.json") == ".claude/settings.json"

    def test_relocated_config_dir_is_blocked(self, _home_outside_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """``CLAUDE_CONFIG_DIR`` で移した置き場のホスト設定も deny する。"""
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_home_outside_repo / ".claude-work"))

        assert (
            bash_config_protection.find_protected_write("printf x > ~/.claude-work/settings.local.json")
            == ".claude/settings.local.json"
        )

    def test_guarded_locations_skip_home_when_undeterminable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """ホームを決められない環境でも落ちず、プラグインの hooks.json は守り続ける。"""

        def _no_home() -> Path:
            """ホーム解決の失敗を模す。"""
            raise OSError("no home")

        monkeypatch.setattr(host_guard_targets, "get_home_dir", _no_home)

        assert [display for _path, display in host_guard_targets._guarded_locations()] == ["hooks/hooks.json/"]

    def test_guarded_locations_skip_unresolvable_config_dir(self, monkeypatch: pytest.MonkeyPatch) -> None:
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


@pytest.mark.parametrize(
    ("command", "denied"),
    [
        ("true;(rm ruff.toml)", True),
        ("(printf x)>.git/hooks/pre-commit", True),
        ("echo x|&tee ruff.toml", True),
        # `<>` は読み書きオープンで、無ければ作成する（空ファイル化と同じ効果）。
        ("printf x <>ruff.toml", True),
        ("printf x 1<> ruff.toml", True),
        # fd 複製・here-string は書込みと読まない。
        ("echo x >&2", False),
        ("cmd 2>&1;(echo ruff.toml)", False),
        ("cat <<<ruff.toml", False),
    ],
    ids=[
        "semicolon-paren",
        "paren-redirect",
        "pipe-amp",
        "read-write-open",
        "fd-read-write-open",
        "fd-dup",
        "fd-dup-then-subshell",
        "herestring",
    ],
)
def test_glued_operator_run_on_parsed_path(command: str, denied: bool) -> None:
    """`shlex` が記号の連なりを 1 語にしても、埋もれた区切り・リダイレクトを読んで保護対象への書込みを deny する。"""
    assert (bash_config_protection.find_protected_write(command) is not None) is denied


@pytest.mark.parametrize("keyword", sorted(_EXPECTED_LEADING_SHELL_KEYWORDS))
def test_directory_change_behind_shell_keywords_is_not_trusted(keyword: str) -> None:
    """予約語・グループ化・`builtin` の後ろの ``cd`` も移動として扱い、相対の ``hooks.json`` を塞ぐ。"""
    command = f"{keyword} cd X/hooks; printf x > hooks.json"

    assert bash_config_protection.find_protected_write(command) == "hooks/hooks.json/"
