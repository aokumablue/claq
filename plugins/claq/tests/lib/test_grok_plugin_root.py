"""claq.lib.grok_plugin_root のテスト。"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from claq.lib import grok_plugin_root as mod


def _make_installed_plugin(home: Path, name: str = "claq-abc123") -> Path:
    """``home/.grok/installed-plugins/<name>`` に launcher 付きツリーを作る。"""
    root = home / ".grok" / "installed-plugins" / name
    launcher = root / "src" / "claq" / "launcher.py"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text("# test\n", encoding="utf-8")
    return root


class TestIsGrokInstalledPluginRoot:
    """is_grok_installed_plugin_root のテスト。"""

    def test_accepts_installed_plugins_claq_hash(self, tmp_path: Path) -> None:
        """installed-plugins/claq-* は True。"""
        root = _make_installed_plugin(tmp_path)
        assert mod.is_grok_installed_plugin_root(root) is True

    def test_rejects_dev_checkout(self, tmp_path: Path) -> None:
        """開発用パスは False。"""
        dev = tmp_path / "dev" / "claq-dev" / "plugins" / "claq"
        (dev / "src" / "claq").mkdir(parents=True)
        (dev / "src" / "claq" / "launcher.py").write_text("#\n", encoding="utf-8")
        assert mod.is_grok_installed_plugin_root(dev) is False

    def test_rejects_grok_plugins_link_path(self, tmp_path: Path) -> None:
        """~/.grok/plugins/claq 自体は installed ではない。"""
        path = tmp_path / ".grok" / "plugins" / "claq"
        path.mkdir(parents=True)
        assert mod.is_grok_installed_plugin_root(path) is False

    def test_resolve_oserror_returns_false(self, tmp_path: Path) -> None:
        """resolve 失敗時は False。"""
        with patch.object(Path, "resolve", side_effect=OSError()):
            assert mod.is_grok_installed_plugin_root(tmp_path) is False

    def test_rejects_grok_without_plugin_name(self, tmp_path: Path) -> None:
        """``.grok`` だけで installed-plugins/claq-* が無いなら False。"""
        grok = tmp_path / ".grok"
        grok.mkdir()
        assert mod.is_grok_installed_plugin_root(grok) is False


class TestFindLatestInstalledClaq:
    """find_latest_installed_claq のテスト。"""

    @pytest.mark.parametrize("newer_name", ["claq-a", "claq-b"])
    def test_returns_newest_with_launcher(self, tmp_path: Path, newer_name: str) -> None:
        """mtime が最も新しい claq-* を返す（名前順・列挙順では決まらない）。

        両候補とも ``claq-`` 接頭辞と launcher を持つので、``startswith`` を見る
        だけでは「最新を選ぶ」という契約は検査できない。``os.utime`` で mtime を
        明確に分け、どちらを新しくしても対応する側が返ることを表として固定する
        （片側だけだと「常に片方を返す」実装が素通りする）。
        """
        installed = tmp_path / "installed"
        roots = {name: installed / name for name in ("claq-a", "claq-b")}
        for root in roots.values():
            launcher = root / "src" / "claq" / "launcher.py"
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text("#\n", encoding="utf-8")
        for name, root in roots.items():
            stamp = 2_000_000_000.0 if name == newer_name else 1_000_000_000.0
            os.utime(root, (stamp, stamp))

        assert mod.find_latest_installed_claq(installed) == roots[newer_name]

    def test_skips_without_launcher(self, tmp_path: Path) -> None:
        """launcher が無いディレクトリは無視する。"""
        (tmp_path / "claq-empty").mkdir()
        assert mod.find_latest_installed_claq(tmp_path) is None

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        """探索先が無ければ None。"""
        assert mod.find_latest_installed_claq(tmp_path / "nope") is None

    def test_skips_non_claq_and_files(self, tmp_path: Path) -> None:
        """claq- 以外のディレクトリとファイルは無視する。"""
        (tmp_path / "other").mkdir()
        (tmp_path / "file.txt").write_text("x", encoding="utf-8")
        assert mod.find_latest_installed_claq(tmp_path) is None

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
            assert mod.find_latest_installed_claq(installed) is None


class TestEnsureGrokPluginRootSymlink:
    """ensure_grok_plugin_root_symlink のテスト。"""

    def test_creates_symlink_to_installed_only(self, tmp_path: Path) -> None:
        """installed-plugins 配下だけをリンク先にする。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()

    def test_ignores_dev_plugin_root_argument(self, tmp_path: Path) -> None:
        """開発ツリーを plugin_root に渡してもリンク先にしない。"""
        dev = tmp_path / "claq-dev" / "plugins" / "claq"
        (dev / "src" / "claq").mkdir(parents=True)
        (dev / "src" / "claq" / "launcher.py").write_text("#\n", encoding="utf-8")
        installed = _make_installed_plugin(tmp_path, "claq-real")
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(
            plugin_root=dev, home=tmp_path, link_path=link
        )
        assert result == installed.resolve()
        assert link.resolve() == installed.resolve()

    def test_explicit_installed_plugin_root_used(self, tmp_path: Path) -> None:
        """installed パスを明示したときそれを使う。"""
        target = _make_installed_plugin(tmp_path, "claq-explicit")
        _make_installed_plugin(tmp_path, "claq-other")
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(
            plugin_root=target, home=tmp_path, link_path=link
        )
        assert result == target.resolve()

    def test_idempotent_when_already_linked(self, tmp_path: Path) -> None:
        """既に正しいリンクなら再作成せず target を返す。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        again = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert again == target.resolve()
        assert link.is_symlink()

    def test_replaces_wrong_symlink(self, tmp_path: Path) -> None:
        """誤った symlink を正しい installed 先に張り替える。"""
        wrong = tmp_path / "wrong"
        wrong.mkdir()
        right = _make_installed_plugin(tmp_path, "claq-right")
        link = tmp_path / ".grok" / "plugins" / "claq"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(wrong)
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == right.resolve()
        assert link.resolve() == right.resolve()

    def test_returns_none_without_installed(self, tmp_path: Path) -> None:
        """installed-plugins が無ければ None（開発ツリーへは落ちない）。"""
        link = tmp_path / ".grok" / "plugins" / "claq"
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert not link.exists()

    def test_plugin_root_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        """明示 plugin_root の resolve 失敗時は探索へ進む。"""
        target = _make_installed_plugin(tmp_path, "claq-explicit")
        real = _make_installed_plugin(tmp_path, "claq-real")
        link = tmp_path / ".grok" / "plugins" / "claq"
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
        empty = tmp_path / ".grok" / "installed-plugins" / "claq-empty"
        empty.mkdir(parents=True)
        real = _make_installed_plugin(tmp_path, "claq-real")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(empty))
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == real.resolve()

    def test_uses_claude_plugin_root_env(self, tmp_path: Path, monkeypatch) -> None:
        """CLAUDE_PLUGIN_ROOT が installed ならそれを使う。"""
        target = _make_installed_plugin(tmp_path, "claq-env")
        _make_installed_plugin(tmp_path, "claq-other")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(target))
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()

    def test_env_root_resolve_oserror_falls_back(self, tmp_path: Path, monkeypatch) -> None:
        """環境変数パスの resolve 失敗時は installed-plugins 探索へ進む。"""
        env_target = _make_installed_plugin(tmp_path, "claq-env")
        real = _make_installed_plugin(tmp_path, "claq-real")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(env_target))
        link = tmp_path / ".grok" / "plugins" / "claq"
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
            if self.name.startswith("claq-"):
                raise OSError()
            return orig(self, strict=strict)

        link = tmp_path / ".grok" / "plugins" / "claq"
        with patch.object(Path, "resolve", fake):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None

    def test_replaces_unresolvable_symlink(self, tmp_path: Path) -> None:
        """既存 symlink の resolve 失敗時は張り替える。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
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
        link = tmp_path / ".grok" / "plugins" / "claq"
        launcher = link / "src" / "claq" / "launcher.py"
        launcher.parent.mkdir(parents=True, exist_ok=True)
        launcher.write_text("#\n", encoding="utf-8")
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert not link.is_symlink()

    def test_replaces_empty_directory(self, tmp_path: Path) -> None:
        """空ディレクトリは削除して symlink にする。"""
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        link.mkdir(parents=True)
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()

    def test_leaves_nonempty_directory(self, tmp_path: Path) -> None:
        """空でない実体ディレクトリは触らない。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        link.mkdir(parents=True)
        (link / "readme").write_text("x", encoding="utf-8")
        assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
        assert link.is_dir() and not link.is_symlink()

    def test_existing_dir_iterdir_oserror(self, tmp_path: Path) -> None:
        """既存ディレクトリの iterdir 失敗時は触らない。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
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
        link = tmp_path / ".grok" / "plugins" / "claq"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("not a dir", encoding="utf-8")
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)
        assert result == target.resolve()
        assert link.is_symlink()

    def test_symlink_oserror_returns_none(self, tmp_path: Path) -> None:
        """symlink 作成失敗時は None。"""
        _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        with patch.object(Path, "symlink_to", side_effect=OSError()):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None

    def test_home_omitted_uses_path_home(self, tmp_path: Path, monkeypatch) -> None:
        """home 未指定時は Path.home()（$HOME）を使う（grok.sh からの実呼び出し経路）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        target = _make_installed_plugin(tmp_path)
        link = tmp_path / ".grok" / "plugins" / "claq"
        result = mod.ensure_grok_plugin_root_symlink(link_path=link)
        assert result == target.resolve()


class TestFindLatestSurvivesStatRace:
    """列挙と stat の間で候補が消えても例外を出さないこと（H-19b）。

    `iterdir()` は try 内だが `candidates.sort(key=lambda p: p.stat().st_mtime)`
    は try の外にあった。Grok が同時にプラグインを更新して古い
    `~/.grok/installed-plugins/claq-<hash>` を消すと `FileNotFoundError` が
    `find_latest_installed_claq`（「例外は発生しません」）→
    `_resolve_symlink_target` → `ensure_grok_plugin_root_symlink`
    （「OSError は握りつぶす」）を貫通し、`scripts/grok.sh` が traceback で
    異常終了していた。
    """

    @staticmethod
    def _stat_raising_for(victim: Path):
        """``victim`` の stat だけ FileNotFoundError にするパッチ関数を返す。"""
        original = Path.stat

        def fake(self: Path, *args: object, **kwargs: object):
            if self == victim:
                raise FileNotFoundError(2, "No such file or directory", str(victim))
            return original(self, *args, **kwargs)

        return fake

    def test_vanished_candidate_is_excluded(self, tmp_path: Path) -> None:
        """stat に失敗した候補は除外し、残りから最新を返すこと。"""
        installed = tmp_path / ".grok" / "installed-plugins"
        survivor = _make_installed_plugin(tmp_path, "claq-survivor")
        vanished = _make_installed_plugin(tmp_path, "claq-vanished")
        # 消える側を新しくして、除外されなければそちらが選ばれる状況にする。
        os.utime(survivor, (1_000, 1_000))
        os.utime(vanished, (2_000, 2_000))

        with patch.object(Path, "stat", self._stat_raising_for(vanished)):
            found = mod.find_latest_installed_claq(installed)

        assert found == survivor

    def test_all_candidates_vanished_returns_none(self, tmp_path: Path) -> None:
        """全候補の stat が失敗しても None を返す（IndexError にしない）。"""
        installed = tmp_path / ".grok" / "installed-plugins"
        only = _make_installed_plugin(tmp_path, "claq-only")

        with patch.object(Path, "stat", self._stat_raising_for(only)):
            assert mod.find_latest_installed_claq(installed) is None

    def test_symlink_helper_does_not_propagate_stat_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """貫通経路の入口（`ensure_grok_plugin_root_symlink`）でも例外にならないこと。

        `scripts/grok.sh` が呼ぶのはこちら。ここが traceback で落ちていた。
        """
        # 実環境の CLAUDE_PLUGIN_ROOT が installed-plugins を指していると
        # `_resolve_symlink_target` が探索前に解決してしまい、判定がぶれる。
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        only = _make_installed_plugin(tmp_path, "claq-only")
        link = tmp_path / ".grok" / "plugins" / "claq"

        with patch.object(Path, "stat", self._stat_raising_for(only)):
            assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
