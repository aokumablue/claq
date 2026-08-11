"""bluecore.lib.grok_plugin_root のテスト。"""

from __future__ import annotations

from pathlib import Path

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
