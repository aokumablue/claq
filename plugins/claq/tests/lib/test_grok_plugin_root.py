"""claq.lib.grok_plugin_root のテスト。"""

from __future__ import annotations

import os
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

import pytest

from claq.lib import grok_plugin_root as mod


@pytest.fixture(autouse=True)
def _no_env_plugin_root(monkeypatch: pytest.MonkeyPatch) -> None:
    """実環境の CLAUDE_PLUGIN_ROOT が installed-plugins を指していると探索前に解決されるので外す。"""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)


@pytest.fixture
def link(tmp_path: Path) -> Path:
    """既定位置 ``~/.grok/plugins/claq`` に当たるリンクのパス。"""
    return tmp_path / ".grok" / "plugins" / "claq"


def _write(path: Path, text: str = "#\n") -> Path:
    """親ディレクトリごと ``path`` にテキストを書いて返す。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _make_installed_plugin(home: Path, name: str = "claq-abc123", mtime: float | None = None) -> Path:
    """``home/.grok/installed-plugins/<name>`` に launcher 付きツリーを作り、``mtime`` があれば設定する。"""
    root = home / ".grok" / "installed-plugins" / name
    _write(root / "src" / "claq" / "launcher.py")
    if mtime is not None:
        os.utime(root, (mtime, mtime))
    return root


def _failing(method: str, should_fail: Callable[[Path], bool], exc: OSError) -> object:
    """``Path.<method>`` を ``should_fail`` に合う self でだけ ``exc`` にするパッチ（コンテキストマネージャ）。"""
    original = getattr(Path, method)

    def fake(self: Path, *args: object, **kwargs: object) -> object:
        """対象の self なら ``exc`` を送出し、それ以外は元の実装へ委ねる。"""
        if should_fail(self):
            raise exc
        return original(self, *args, **kwargs)

    return patch.object(Path, method, fake)


def _from_second_call(name: str) -> Callable[[Path], bool]:
    """``name`` を含むパスについて、同じパスの 2 回目以降の呼び出しで真になる判定を返す。

    1 回目は is_grok_installed_plugin_root の判定、2 回目は採用時の resolve に当たる。
    """
    counts: Counter[str] = Counter()

    def should_fail(path: Path) -> bool:
        """``name`` を含むパスの呼び出し回数を数え、2 回目以降で真を返す。"""
        if name not in path.parts:
            return False
        counts[str(path)] += 1
        return counts[str(path)] >= 2

    return should_fail


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        (".grok/installed-plugins/claq-abc123", True),
        ("dev/claq-dev/plugins/claq", False),
        (".grok/plugins/claq", False),
        (".grok", False),
    ],
    ids=["installed-claq-hash", "dev-checkout", "grok-plugins-link-path", "grok-without-plugin-name"],
)
def test_is_grok_installed_plugin_root(tmp_path: Path, relative: str, expected: bool) -> None:
    """``.grok/installed-plugins/claq-*`` 配下だけが installed と判定される。"""
    path = tmp_path / relative
    path.mkdir(parents=True)
    assert mod.is_grok_installed_plugin_root(path) is expected


def test_is_grok_installed_plugin_root_resolve_oserror_returns_false(tmp_path: Path) -> None:
    """resolve 失敗時は False。"""
    with patch.object(Path, "resolve", side_effect=OSError()):
        assert mod.is_grok_installed_plugin_root(tmp_path) is False


class TestFindLatestInstalledClaq:
    """find_latest_installed_claq のテスト。

    列挙と stat の間で候補が消える（Grok が同時に古い ``claq-<hash>`` を消す）と、stat を try の外で
    行う実装では FileNotFoundError が ensure_grok_plugin_root_symlink（「OSError は握りつぶす」）を
    貫通し、``scripts/grok.sh`` が traceback で異常終了する。
    """

    @pytest.mark.parametrize("newer_name", ["claq-a", "claq-b"])
    def test_returns_newest_with_launcher(self, tmp_path: Path, newer_name: str) -> None:
        """mtime が最も新しい claq-* を返す（名前順・列挙順では決まらない）。

        どちらを新しくしても対応する側が返ることを表として固定する（片側だけだと「常に片方を返す」実装が素通りする）。
        """
        roots = {
            name: _make_installed_plugin(tmp_path, name, 2_000_000_000.0 if name == newer_name else 1_000_000_000.0)
            for name in ("claq-a", "claq-b")
        }

        assert mod.find_latest_installed_claq(tmp_path / ".grok" / "installed-plugins") == roots[newer_name]

    def test_ignores_non_candidates(self, tmp_path: Path) -> None:
        """launcher の無い claq-*・claq- 以外の名前のディレクトリ・ファイルは、それぞれ単独の条件で除外される。"""
        (tmp_path / "claq-empty").mkdir()
        _write(tmp_path / "other" / "src" / "claq" / "launcher.py")
        _write(tmp_path / "file.txt", "x")

        assert mod.find_latest_installed_claq(tmp_path) is None

    def test_missing_dir_returns_none(self, tmp_path: Path) -> None:
        """探索先が無ければ None。"""
        assert mod.find_latest_installed_claq(tmp_path / "nope") is None

    def test_iterdir_oserror_returns_none(self, tmp_path: Path) -> None:
        """探索先の iterdir が OSError なら None。"""
        installed = tmp_path / "installed"
        installed.mkdir()

        with _failing("iterdir", lambda path: path == installed, OSError()):
            assert mod.find_latest_installed_claq(installed) is None

    def test_vanished_candidate_is_excluded(self, tmp_path: Path) -> None:
        """stat に失敗した候補は除外し、残りから最新を返す（消える側を新しくして、除外されなければ選ばれる状況にする）。"""
        survivor = _make_installed_plugin(tmp_path, "claq-survivor", 1_000)
        vanished = _make_installed_plugin(tmp_path, "claq-vanished", 2_000)

        with _failing("stat", lambda path: path == vanished, FileNotFoundError(2, "gone")):
            assert mod.find_latest_installed_claq(tmp_path / ".grok" / "installed-plugins") == survivor

    def test_all_candidates_vanished_returns_none(self, tmp_path: Path) -> None:
        """全候補の stat が失敗しても None を返す（IndexError にしない）。"""
        only = _make_installed_plugin(tmp_path, "claq-only")

        with _failing("stat", lambda path: path == only, FileNotFoundError(2, "gone")):
            assert mod.find_latest_installed_claq(tmp_path / ".grok" / "installed-plugins") is None


@pytest.mark.parametrize(
    ("plugin_root", "env_root", "expected"),
    [
        ("claq-explicit", None, "claq-explicit"),
        (None, "claq-env", "claq-env"),
        ("claq-explicit", "claq-env", "claq-explicit"),
        ("dev", None, "claq-newest"),
        (None, "claq-empty", "claq-newest"),
    ],
    ids=["explicit-installed", "env-installed", "explicit-before-env", "dev-tree-ignored", "env-without-launcher"],
)
def test_symlink_target_resolution_order(
    tmp_path: Path,
    link: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_root: str | None,
    env_root: str | None,
    expected: str,
) -> None:
    """リンク先は plugin_root → CLAUDE_PLUGIN_ROOT → 最新インストールの順で、installed かつ launcher 付きだけを採る。

    最新の claq-newest を囮にして、明示・環境変数の行が「最新を選んだだけ」で通らないようにする。
    """
    _make_installed_plugin(tmp_path, "claq-explicit", 1_000)
    _make_installed_plugin(tmp_path, "claq-env", 1_000)
    _make_installed_plugin(tmp_path, "claq-newest", 3_000)
    (tmp_path / ".grok" / "installed-plugins" / "claq-empty").mkdir()
    dev = _write(tmp_path / "dev" / "plugins" / "claq" / "src" / "claq" / "launcher.py").parents[2]

    def locate(name: str) -> Path:
        """行の名前を dev ツリーまたは installed-plugins 配下のパスへ変換する。"""
        return dev if name == "dev" else tmp_path / ".grok" / "installed-plugins" / name

    if env_root is not None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(locate(env_root)))
    root_arg = locate(plugin_root) if plugin_root is not None else None

    result = mod.ensure_grok_plugin_root_symlink(plugin_root=root_arg, home=tmp_path, link_path=link)

    assert result == locate(expected).resolve()
    assert link.resolve() == result


@pytest.mark.parametrize("source", ["plugin_root", "env"])
def test_candidate_resolve_oserror_falls_back_to_latest(
    tmp_path: Path, link: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    """明示 plugin_root・CLAUDE_PLUGIN_ROOT の採用時 resolve が失敗したら installed-plugins 探索へ進む。"""
    candidate = _make_installed_plugin(tmp_path, "claq-candidate", 1_000)
    real = _make_installed_plugin(tmp_path, "claq-real", 2_000)
    if source == "env":
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(candidate))
    root_arg = candidate if source == "plugin_root" else None

    with _failing("resolve", _from_second_call(candidate.name), OSError()):
        result = mod.ensure_grok_plugin_root_symlink(plugin_root=root_arg, home=tmp_path, link_path=link)

    assert result == real.resolve()


def test_returns_none_without_installed(tmp_path: Path, link: Path) -> None:
    """installed-plugins が無ければ None で、リンクも作らない（開発ツリーへは落ちない）。"""
    assert mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link) is None
    assert not link.exists()


def test_home_omitted_uses_path_home(tmp_path: Path, link: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """home 未指定時は Path.home()（$HOME）を使う（grok.sh からの実呼び出し経路）。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    target = _make_installed_plugin(tmp_path)

    assert mod.ensure_grok_plugin_root_symlink(link_path=link) == target.resolve()


@pytest.mark.parametrize(
    ("prepare", "replaced"),
    [
        (None, True),
        (lambda link, target: link.symlink_to(target, target_is_directory=True), True),
        (lambda link, target: link.symlink_to(target.parent, target_is_directory=True), True),
        (lambda link, target: link.mkdir(), True),
        (lambda link, target: _write(link, "not a dir"), True),
        (lambda link, target: _write(link / "readme", "x"), False),
        (lambda link, target: _write(link / "src" / "claq" / "launcher.py"), False),
    ],
    ids=["absent", "already-linked", "wrong-symlink", "empty-dir", "regular-file", "nonempty-dir", "launcher-tree"],
)
def test_existing_link_state(
    tmp_path: Path, link: Path, prepare: Callable[[Path, Path], object] | None, replaced: bool
) -> None:
    """リンク位置の既存物ごとに、張る・張り替える・触らないが決まる（空でない実体ディレクトリは触らない）。"""
    target = _make_installed_plugin(tmp_path)
    if prepare is not None:
        link.parent.mkdir(parents=True)
        prepare(link, target)

    result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)

    if replaced:
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()
    else:
        assert result is None
        assert link.is_dir() and not link.is_symlink()


@pytest.mark.parametrize(
    ("prepare", "method", "should_fail", "exc", "replaced"),
    [
        (None, "resolve", lambda path, link: path.name.startswith("claq-"), OSError(), False),
        (None, "stat", lambda path, link: path.name.startswith("claq-"), FileNotFoundError(2, "gone"), False),
        (None, "symlink_to", lambda path, link: True, OSError(), False),
        (lambda link: link.symlink_to(link.parent / "missing"), "resolve", lambda path, link: path == link, OSError(), True),
        (lambda link: link.mkdir(), "iterdir", lambda path, link: path == link, OSError(), False),
    ],
    ids=["found-resolve-fails", "candidate-vanishes", "symlink-to-fails", "unresolvable-symlink", "dir-iterdir-fails"],
)
def test_os_errors_are_swallowed(
    tmp_path: Path,
    link: Path,
    prepare: Callable[[Path], object] | None,
    method: str,
    should_fail: Callable[[Path, Path], bool],
    exc: OSError,
    replaced: bool,
) -> None:
    """途中の OSError は例外にせず None を返す。既存 symlink の resolve 失敗だけは張り替える。

    candidate-vanishes は stat 競合が入口（grok.sh が呼ぶ ensure_grok_plugin_root_symlink）まで貫通しないことを見る。
    """
    target = _make_installed_plugin(tmp_path)
    if prepare is not None:
        link.parent.mkdir(parents=True)
        prepare(link)

    with _failing(method, lambda path: should_fail(path, link), exc):
        result = mod.ensure_grok_plugin_root_symlink(home=tmp_path, link_path=link)

    if replaced:
        assert result == target.resolve()
        assert link.is_symlink()
        assert link.resolve() == target.resolve()
    else:
        assert result is None
