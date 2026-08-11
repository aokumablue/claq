"""bluecore.lib.grok_plugin_root のテスト。"""

from __future__ import annotations

from pathlib import Path

from bluecore.lib import grok_plugin_root as mod


def _make_plugin_tree(root: Path) -> Path:
    """launcher.py を含む最小プラグインツリーを作る。"""
    launcher = root / "src" / "bluecore" / "launcher.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("# test\n", encoding="utf-8")
    return root


class TestFindLatestInstalledBluecore:
    """find_latest_installed_bluecore のテスト。"""

    def test_returns_newest_with_launcher(self, tmp_path: Path) -> None:
        """launcher を持つ最新 bluecore-* を返す。"""
        old = _make_plugin_tree(tmp_path / "bluecore-old")
        new = _make_plugin_tree(tmp_path / "bluecore-new")
        # mtime を new の方が新しく
        Path(old / "src" / "bluecore" / "launcher.py").touch()
        Path(new / "src" / "bluecore" / "launcher.py").touch()
        found = mod.find_latest_installed_bluecore(tmp_path)
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

    def test_creates_symlink_to_plugin_root(self, tmp_path: Path) -> None:
        """plugin_root 指定で link を作成する。"""
        target = _make_plugin_tree(tmp_path / "installed" / "bluecore-abc")
        link = tmp_path / "plugins" / "bluecore"
        result = mod.ensure_grok_plugin_root_symlink(plugin_root=target, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()
        assert (link / "src" / "bluecore" / "launcher.py").is_file()

    def test_idempotent_when_already_linked(self, tmp_path: Path) -> None:
        """既に正しいリンクなら再作成せず target を返す。"""
        target = _make_plugin_tree(tmp_path / "bluecore-abc")
        link = tmp_path / "bluecore"
        mod.ensure_grok_plugin_root_symlink(plugin_root=target, link_path=link)
        again = mod.ensure_grok_plugin_root_symlink(plugin_root=target, link_path=link)
        assert again == target.resolve()
        assert link.is_symlink()

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        """誤った symlink を正しい target に張り替える。"""
        wrong = _make_plugin_tree(tmp_path / "wrong")
        right = _make_plugin_tree(tmp_path / "right")
        link = tmp_path / "bluecore"
        link.symlink_to(wrong)
        result = mod.ensure_grok_plugin_root_symlink(plugin_root=right, link_path=link)
        assert result == right.resolve()
        assert link.resolve() == right.resolve()

    def test_does_not_clobber_valid_real_tree(self, tmp_path: Path) -> None:
        """launcher がある実ディレクトリは上書きしない。"""
        real = _make_plugin_tree(tmp_path / "bluecore")
        other = _make_plugin_tree(tmp_path / "other")
        result = mod.ensure_grok_plugin_root_symlink(plugin_root=other, link_path=real)
        assert result is None
        assert not real.is_symlink()
        assert (real / "src" / "bluecore" / "launcher.py").is_file()

    def test_returns_none_without_target(self, tmp_path: Path) -> None:
        """有効な target が無ければ None。"""
        link = tmp_path / "plugins" / "bluecore"
        assert mod.ensure_grok_plugin_root_symlink(plugin_root=tmp_path / "nope", link_path=link) is None
