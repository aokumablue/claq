"""grok.sh のテスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "plugins" / "claq" / "scripts" / "grok.sh"


def _run_script(home: Path) -> subprocess.CompletedProcess[str]:
    """``sh grok.sh`` を偽 ``$HOME`` で実行する。"""
    env = {key: value for key, value in os.environ.items() if key != "CLAUDE_PLUGIN_ROOT"}
    return subprocess.run(["sh", str(_SCRIPT)], capture_output=True, text=True, env={**env, "HOME": str(home)})


def _make_installed_plugin(home: Path, name: str) -> Path:
    """``home/.grok/installed-plugins/<name>`` に実ソース一式付きツリーを作る。

    grok.sh は ``src/claq/lib/grok_plugin_root.py`` の実在を見て ``PYTHONPATH`` を組み立てるため、
    import 可能な実体が要る。リポジトリの実 src を symlink して賄う（重複コピーはしない）。
    """
    root = home / ".grok" / "installed-plugins" / name
    root.mkdir(parents=True)
    (root / "src").symlink_to(_REPO_ROOT / "plugins" / "claq" / "src", target_is_directory=True)
    return root


def test_links_installed_plugin_and_rerun_is_idempotent(tmp_path: Path) -> None:
    """installed-plugins/claq-* があれば symlink を張って linked -> を出し、再実行してもエラーにならない。"""
    target = _make_installed_plugin(tmp_path, "claq-abc123")
    link = tmp_path / ".grok" / "plugins" / "claq"

    for _ in range(2):
        result = _run_script(tmp_path)

        assert result.returncode == 0, result.stderr
        assert "linked ->" in result.stdout
        assert link.is_symlink()
        assert link.resolve() == target.resolve()


def test_no_installed_plugin_exits_nonzero_without_link(tmp_path: Path) -> None:
    """installed-plugins が無ければ symlink を作らず非 0 で終わる（exit 0 だと呼び出し側が成功と区別できない）。"""
    result = _run_script(tmp_path)

    assert "no installed claq-* found" in result.stderr
    assert result.returncode != 0
    assert not (tmp_path / ".grok" / "plugins" / "claq").exists()


def test_picks_newest_install_like_the_python_resolver(tmp_path: Path) -> None:
    """複数インストールが同居する場合、辞書順の先頭・末尾ではなく mtime 最新を選ぶこと。

    シェル側が glob の端を採ると、実行される bootstrap コードと symlink の張り先が別バージョンになりうる。
    """
    installs = {name: _make_installed_plugin(tmp_path, name) for name in ("claq-aaa111", "claq-mmm555", "claq-zzz999")}
    # mtime 最新を辞書順の中央に置く。
    for name, mtime in (("claq-aaa111", 1_700_000_000), ("claq-zzz999", 1_750_000_000), ("claq-mmm555", 1_800_000_000)):
        os.utime(installs[name], (mtime, mtime))

    result = _run_script(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".grok" / "plugins" / "claq").resolve() == installs["claq-mmm555"].resolve()
