"""Grok Build のプラグインルートと hooks が期待するパスを橋渡しする。

Grok は ``${CLAUDE_PLUGIN_ROOT}`` を ``~/.grok/plugins/<name>`` に展開するが、
実体は ``~/.grok/installed-plugins/<name>-<hash>/`` に置かれる。hooks.json の
launcher パスが前者を参照するため、インストール時と SessionStart で
シンボリックリンクを張って一致させる。

リンク先は常に ``~/.grok/installed-plugins/bluecore-*`` のみとする。
開発用チェックアウト（例: bluecore-dev）は一切参照しない。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def _home() -> Path:
    """ホームディレクトリを返す。"""
    return Path.home()


def installed_plugins_dir(home: Path | None = None) -> Path:
    """``~/.grok/installed-plugins`` のパスを返す。

    Args:
        home: ホームディレクトリ上書き（テスト用）。None なら Path.home()。

    Returns:
        installed-plugins ディレクトリの Path。
    """
    base = home if home is not None else _home()
    return base / ".grok" / "installed-plugins"


def is_grok_installed_plugin_root(path: Path | str) -> bool:
    """パスが Grok の公式インストール先（installed-plugins/bluecore-*）か判定する。

    Args:
        path: 検査対象パス。

    Returns:
        installed-plugins 配下の bluecore-* ツリーなら True。

    Raises:
        例外は発生しません。
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    parts = resolved.parts
    try:
        grok_idx = parts.index(".grok")
    except ValueError:
        return False
    if grok_idx + 2 >= len(parts):
        return False
    if parts[grok_idx + 1] != "installed-plugins":
        return False
    return parts[grok_idx + 2].startswith("bluecore-")


def _has_launcher(root: Path) -> bool:
    """プラグインルートに launcher.py があるか。"""
    return (root / "src" / "bluecore" / "launcher.py").is_file()


def find_latest_installed_bluecore(installed_dir: Path | None = None) -> Path | None:
    """``~/.grok/installed-plugins/bluecore-*`` のうち最新のディレクトリを返す。

    Args:
        installed_dir: 探索先。None なら ``~/.grok/installed-plugins``。

    Returns:
        launcher.py を含む最新ディレクトリ。無ければ None。

    Raises:
        例外は発生しません。
    """
    root = installed_dir if installed_dir is not None else installed_plugins_dir()
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for path in root.iterdir():
            if not path.is_dir():
                continue
            if not path.name.startswith("bluecore-"):
                continue
            if _has_launcher(path):
                candidates.append(path)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def ensure_venv_symlink_for_installed(
    *,
    home: Path | None = None,
    shared_venv: Path | str | None = None,
) -> list[Path]:
    """``installed-plugins/bluecore-*`` 各ツリーに共有 venv への .venv symlink を張る。

    実体 venv は常に ``~/.bluecore/.venv``（または shared_venv）1 つ。
    各インストールディレクトリには symlink のみ置く（Claude/Copilot と同じ）。

    Args:
        home: ホーム上書き（テスト用）。
        shared_venv: 共有 venv パス。None なら ``~/.bluecore/.venv``。

    Returns:
        新たに作成または張り替えた .venv リンクの Path リスト。

    Raises:
        例外は発生しません。
    """
    base = home if home is not None else _home()
    venv = Path(shared_venv) if shared_venv is not None else base / ".bluecore" / ".venv"
    installed = installed_plugins_dir(base)
    if not installed.is_dir():
        return []

    updated: list[Path] = []
    try:
        children = list(installed.iterdir())
    except OSError:
        return []

    venv_str = str(venv)
    for path in children:
        if not path.is_dir() or not path.name.startswith("bluecore-"):
            continue
        if not _has_launcher(path):
            continue
        link = path / ".venv"
        try:
            if link.is_symlink():
                # install.sh と同様: readlink 文字列一致ならスキップ
                if os.readlink(link) == venv_str:
                    continue
                link.unlink()
            elif link.exists():
                # 誤って置かれた実体 venv は置換、それ以外は触らない
                if (link / "pyvenv.cfg").is_file():
                    shutil.rmtree(link)
                else:
                    continue
            link.symlink_to(venv, target_is_directory=True)
            updated.append(link)
        except OSError:
            continue
    return updated


def ensure_grok_plugin_root_symlink(
    *,
    plugin_root: Path | str | None = None,
    link_path: Path | str | None = None,
    home: Path | None = None,
) -> Path | None:
    """``~/.grok/plugins/bluecore`` を **installed-plugins 配下のみ** へ symlink する。

    開発用リポジトリや任意パスはリンク先にしない。明示の ``plugin_root`` も
    ``is_grok_installed_plugin_root`` を満たす場合だけ採用する。

    Args:
        plugin_root: 候補のリンク先。installed-plugins/bluecore-* でなければ無視。
        link_path: リンクのパス。None なら ``~/.grok/plugins/bluecore``。
        home: ホーム上書き（テスト用）。

    Returns:
        作成・更新したリンク先 Path。何もしなければ None。

    Raises:
        例外は発生しません（OSError は握りつぶす）。
    """
    base = home if home is not None else _home()
    link = Path(link_path) if link_path is not None else base / ".grok" / "plugins" / "bluecore"

    target: Path | None = None
    if plugin_root is not None:
        candidate = Path(plugin_root)
        if is_grok_installed_plugin_root(candidate) and _has_launcher(candidate):
            try:
                target = candidate.resolve()
            except OSError:
                target = None
        # 開発ツリー等は黙って無視し、installed-plugins 探索へ進む

    if target is None:
        env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
        if env_root and is_grok_installed_plugin_root(env_root):
            candidate = Path(env_root)
            if _has_launcher(candidate):
                try:
                    target = candidate.resolve()
                except OSError:
                    target = None

    if target is None:
        found = find_latest_installed_bluecore(installed_plugins_dir(base))
        if found is not None:
            try:
                target = found.resolve()
            except OSError:
                target = None

    if target is None:
        return None

    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink():
            try:
                if link.resolve() == target:
                    return target
            except OSError:
                pass
            link.unlink()
        elif link.exists():
            # 既に有効なツリーなら触らない
            if _has_launcher(link):
                return None
            if link.is_dir():
                try:
                    next(link.iterdir())
                except StopIteration:
                    link.rmdir()
                except OSError:
                    return None
                else:
                    return None
            else:
                link.unlink()
        link.symlink_to(target, target_is_directory=True)
        return target
    except OSError:
        return None
