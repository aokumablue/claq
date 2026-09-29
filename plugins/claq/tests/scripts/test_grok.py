"""grok.sh のテスト。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SCRIPT = _REPO_ROOT / "plugins" / "claq" / "scripts" / "grok.sh"


def _run_script(home: Path, *, check: bool = True) -> subprocess.CompletedProcess[str]:
    """``sh grok.sh`` を偽 ``$HOME`` で実行する。"""
    env = {**os.environ, "HOME": str(home)}
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return subprocess.run(
        ["sh", str(_SCRIPT)],
        check=check,
        capture_output=True,
        text=True,
        env=env,
    )


def _make_installed_plugin(home: Path, name: str = "claq-abc123") -> Path:
    """``home/.grok/installed-plugins/<name>`` に実ソース一式付きツリーを作る。

    grok.sh は ``src/claq/lib/grok_plugin_root.py`` の実在を
    見て ``PYTHONPATH`` を組み立てるため、import 可能な実体が要る。
    リポジトリの実 src を symlink して賄う（重複コピーはしない）。
    """
    root = home / ".grok" / "installed-plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    real_src = _REPO_ROOT / "plugins" / "claq" / "src"
    (root / "src").symlink_to(real_src, target_is_directory=True)
    return root


def test_links_when_installed_plugin_exists(tmp_path: Path) -> None:
    """installed-plugins/claq-* があれば symlink を張り linked -> を出力する。"""
    target = _make_installed_plugin(tmp_path)
    result = _run_script(tmp_path)

    link = tmp_path / ".grok" / "plugins" / "claq"
    assert "linked ->" in result.stdout
    assert link.is_symlink()
    assert link.resolve() == target.resolve()


def test_no_installed_plugin_exits_nonzero_without_link(tmp_path: Path) -> None:
    """installed-plugins が無ければ symlink を作らず非 0 で終わる。

    以前は「何もできなかった」場合も exit 0 だったため、呼び出し側が成功と
    区別できなかった（F-12）。
    """
    result = _run_script(tmp_path, check=False)

    link = tmp_path / ".grok" / "plugins" / "claq"
    assert "no installed claq-* found" in result.stderr
    assert result.returncode != 0
    assert not link.exists()


def test_picks_newest_install_like_the_python_resolver(tmp_path: Path) -> None:
    """複数インストールが同居する場合、mtime 最新を選ぶこと。

    シェル側が glob 先頭（辞書順）を採っていた頃は、実行される bootstrap
    コードと symlink の張り先が別バージョンになりえた（F-12）。
    """
    older = _make_installed_plugin(tmp_path, name="claq-aaa111")
    newer = _make_installed_plugin(tmp_path, name="claq-zzz999")
    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    result = _run_script(tmp_path)

    link = tmp_path / ".grok" / "plugins" / "claq"
    assert result.returncode == 0
    assert link.resolve() == newer.resolve()


def test_rerun_is_idempotent(tmp_path: Path) -> None:
    """既に symlink がある状態で再実行してもエラーにならない。"""
    target = _make_installed_plugin(tmp_path)
    _run_script(tmp_path)
    result = _run_script(tmp_path)

    link = tmp_path / ".grok" / "plugins" / "claq"
    assert "linked ->" in result.stdout
    assert result.returncode == 0
    assert link.resolve() == target.resolve()
