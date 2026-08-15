"""bluecore.lib.grok_plugin_root のテスト。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from bluecore.lib import grok_plugin_root as mod


def _make_installed_plugin(home: Path, name: str = "bluecore-abc123") -> Path:
    """``home/.grok/installed-plugins/<name>`` に launcher 付きツリーを作る。"""
    root = home / ".grok" / "installed-plugins" / name
    launcher = root / "src" / "bluecore" / "launcher.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("# test\n", encoding="utf-8")
    return root


class TestIsGrokInstalledPluginRoot:
    """is_grok_installed_plugin_root のテスト。"""

    def test_accepts_installed_plugins_bluecore_hash(self, tmp_path: Path) -> None:
        """installed-plugins/bluecore-* は True。"""
        root = _make_installed_plugin(tmp_path)
        assert mod.is_grok_installed_plugin_root(root) is True

    def test_rejects_dev_checkout(self, tmp_path: Path) -> None:
        """開発用パスは False。"""
        dev = tmp_path / "dev" / "bluecore-dev" / "plugins" / "bluecore"
        (dev / "src" / "bluecore").mkdir(parents=True)
        (dev / "src" / "bluecore" / "launcher.py").write_text("#\n", encoding="utf-8")
        assert mod.is_grok_installed_plugin_root(dev) is False

    def test_rejects_grok_plugins_link_path(self, tmp_path: Path) -> None:
        """~/.grok/plugins/bluecore 自体は installed ではない。"""
        path = tmp_path / ".grok" / "plugins" / "bluecore"
        path.mkdir(parents=True)
        assert mod.is_grok_installed_plugin_root(path) is False

    def test_resolve_oserror_returns_false(self, tmp_path: Path) -> None:
        """resolve 失敗時は False。"""
        with patch.object(Path, "resolve", side_effect=OSError()):
            assert mod.is_grok_installed_plugin_root(tmp_path) is False

    def test_rejects_grok_without_plugin_name(self, tmp_path: Path) -> None:
        """``.grok`` だけで installed-plugins/bluecore-* が無いなら False。"""
        grok = tmp_path / ".grok"
        grok.mkdir()
        assert mod.is_grok_installed_plugin_root(grok) is False


class TestFindLatestInstalledBluecore:
    """find_latest_installed_bluecore のテスト。"""

    def test_returns_newest_with_launcher(self, tmp_path: Path) -> None:
        """launcher を持つ最新 bluecore-* を返す。"""
        installed = tmp_path / "installed"
        old = installed / "bluecore-old"
        new = installed / "bluecore-new"
        for root in (old, new):
            launcher = root / "src" / "bluecore" / "launcher.py"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("#\n", encoding="utf-8")
        found = mod.find_latest_installed_bluecore(installed)
        assert found is not None
        assert found.name.startswith("bluecore-")

    def test_skips_without_launcher(self, tmp_path: Path) -> None:
        """launcher が無いディレクトリは無視する。"""
        (tmp_path / "bluecore-empty").mkdir()
        assert mod.find_latest_installed_bluecore(tmp_path) is None

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        """探索先が無ければ None。"""
        assert mod.find_latest_installed_bluecore(tmp_path / "nope") is None

    def test_skips_non_bluecore_and_files(self, tmp_path: Path) -> None:
        """bluecore- 以外のディレクトリとファイルは無視する。"""
        (tmp_path / "other").mkdir()
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        assert mod.find_latest_installed_bluecore(tmp_path) is None

    def test_iterdir_oserror_returns_none(self, tmp_path: Path) -> None:
        """iterdir が OSError なら None。"""
        installed = tmp_path / "installed"
        installed.mkdir()
        orig = Path.iterdir

        def fake(self: Path):
            if self == installed:
                raise OSError()
            return orig(self)

        with patch.object(Path, "iterdir", fake):
            assert mod.find_latest_installed_bluecore(installed) is None


class TestEnsureGrokPluginRootSymlink:
    """ensure_grok_plugin_root_symlink のテスト。"""

    def test_creates_symlink_to_installed_only(self, tmp_path: Path) -> None:
        """installed-plugins 配下だけをリンク先にする。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_ignores_dev_plugin_root_argument(self, tmp_path: Path) -> None:
        """開発ツリーを plugin_root に渡してもリンク先にしない。"""
        dev = tmp_path / "bluecore-dev" / "plugins" / "bluecore"
        (dev / "src" / "bluecore").mkdir(parents=True)
        (dev / "src" / "bluecore" / "launcher.py").write_text("#\n", encoding="utf-8")
        installed = _make_installed_plugin(tmp_path, "bluecore-real")
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(
            plugin_root=dev, home=tmp_path, link_path=link
        )
        assert result == installed.resolve()
        assert link.resolve() == installed.resolve()

    def test_explicit_installed_plugin_root_used(self, tmp_path: Path) -> None:
        """installed パスを明示したときそれを使う。"""
        target = _make_installed_plugin(tmp_path, "bluecore-explicit")
        _make_installed_plugin(tmp_path, "bluecore-other")
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(
            plugin_root=target, home=tmp_path, link_path=link
        )
        assert result == target.resolve()

    def test_idempotent_when_already_linked(self, tmp_path: Path) -> None:
        """既に正しいリンクなら再作成せず target を返す。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        again = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert again == target.resolve()
        assert link.is_symlink()

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        """誤った symlink を正しい installed 先に張り替える。"""
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        right = _make_installed_plugin(tmp_path, "bluecore-right")
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(wrong)
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == right.resolve()
        assert link.resolve() == right.resolve()

    def test_returns_none_without_installed(self, tmp_path: Path) -> None:
        """installed-plugins が無ければ None（開発ツリーへは落ちない）。"""
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert not link.exists()

    def test_plugin_root_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        """明示 plugin_root の resolve 失敗時は探索へ進む。"""
        target = _make_installed_plugin(tmp_path, "bluecore-explicit")
        real = _make_installed_plugin(tmp_path, "bluecore-real")
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        orig = Path.resolve
        counts: dict[str, int] = {}

        def fake(self: Path, strict: bool = False):
            key = str(self)
            counts[key] = counts.get(key, 0) + 1
            if target.name in self.parts and counts[key] >= 2:
                raise OSError()
            return orig(self, strict=strict)

        with patch.object(Path, "resolve", fake):
            result = mod.ensure_grok_plugin_root_symlink(
                plugin_root=target, home=tmp_path, link_path=link
            )
        assert result == real.resolve()

    def test_env_root_without_launcher_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        """CLAUDE_PLUGIN_ROOT が installed でも launcher が無いなら探索へ進む。"""
        empty = tmp_path / ".grok" / "installed-plugins" / "bluecore-empty"
        empty.mkdir(parents=True)
        real = _make_installed_plugin(tmp_path, "bluecore-real")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty))
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == real.resolve()

    def test_uses_claude_plugin_root_env(self, tmp_path: Path, monkeypatch) -> None:
        """CLAUDE_PLUGIN_ROOT が installed ならそれを使う。"""
        target = _make_installed_plugin(tmp_path, "bluecore-env")
        _make_installed_plugin(tmp_path, "bluecore-other")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(target))
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()

    def test_env_root_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        """環境変数パスの resolve 失敗時は installed-plugins 探索へ進む。"""
        env_target = _make_installed_plugin(tmp_path, "bluecore-env")
        real = _make_installed_plugin(tmp_path, "bluecore-real")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env_target))
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        orig = Path.resolve
        counts: dict[str, int] = {}

        def fake(self: Path, strict: bool = False):
            key = str(self)
            counts[key] = counts.get(key, 0) + 1
            if env_target.name in self.parts and counts[key] >= 2:
                raise OSError()
            return orig(self, strict=strict)

        with patch.object(Path, "resolve", fake):
            result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == real.resolve()

    def test_found_resolve_oserror_returns_none(self, tmp_path: Path, monkeypatch) -> None:
        """探索結果の resolve 失敗時は None。"""
        _make_installed_plugin(tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        orig = Path.resolve

        def fake(self: Path, strict: bool = False):
            if self.name.startswith("bluecore-"):
                raise OSError()
            return orig(self, strict=strict)

        link = tmp_path / ".grok" / "plugins" / "bluecore"
        with patch.object(Path, "resolve", fake):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None

    def test_replaces_unresolvable_symlink(self, tmp_path: Path) -> None:
        """既存 symlink の resolve 失敗時は張り替える。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(tmp_path / "missing")
        orig = Path.resolve

        def fake(self: Path, strict: bool = False):
            if self == link:
                raise OSError()
            return orig(self, strict=strict)

        with patch.object(Path, "resolve", fake):
            result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_leaves_existing_launcher_tree(self, tmp_path: Path) -> None:
        """リンク先に既に launcher 付き実体があれば触らない。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        launcher = link / "src" / "bluecore" / "launcher.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#\n", encoding="utf-8")
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert not link.is_symlink()

    def test_replaces_empty_directory(self, tmp_path: Path) -> None:
        """空ディレクトリは削除して symlink にする。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.mkdir(parents=True)
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()

    def test_leaves_nonempty_directory(self, tmp_path: Path) -> None:
        """空でない実体ディレクトリは触らない。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.mkdir(parents=True)
        (link / "readme").write_text("x", encoding="utf-8")
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert link.is_dir() and not link.is_symlink()

    def test_existing_dir_iterdir_oserror(self, tmp_path: Path) -> None:
        """既存ディレクトリの iterdir 失敗時は触らない。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.mkdir(parents=True)
        orig = Path.iterdir

        def fake(self: Path):
            if self == link:
                raise OSError()
            return orig(self)

        with patch.object(Path, "iterdir", fake):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None

    def test_replaces_regular_file(self, tmp_path: Path) -> None:
        """リンク位置の通常ファイルは置き換える。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("not a dir", encoding="utf-8")
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()

    def test_symlink_oserror_returns_none(self, tmp_path: Path) -> None:
        """symlink 作成失敗時は None。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        with patch.object(Path, "symlink_to", side_effect=OSError()):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None

    def test_home_omitted_uses_path_home(self, tmp_path: Path, monkeypatch) -> None:
        """home 未指定時は Path.home()（$HOME）を使う（link_grok_plugin.sh からの実呼び出し経路）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(link_path=link)
        assert result == target.resolve()
