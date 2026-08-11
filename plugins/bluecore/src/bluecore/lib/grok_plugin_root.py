"""Grok Build のプラグインルートと hooks が期待するパスを橋渡しする。

Grok は ``${CLAUDE_PLUGIN_ROOT}`` を ``~/.grok/plugins/<name>`` に展開するが、
実体は ``~/.grok/installed-plugins/<name>-<hash>/`` に置かれる。hooks.json の
launcher パスが前者を参照するため、インストール時と SessionStart で
シンボリックリンクを張って一致させる。
"""

from __future__ import annotations

import os
from pathlib import Path


def _home() -> Path:
    """ホームディレクトリを返す。"""
    return Path.home()


def find_latest_installed_bluecore(installed_dir: Path | None = None) -> Path | None:
    """``~/.grok/installed-plugins/bluecore-*`` のうち最新のディレクトリを返す。

    Args:
        installed_dir: 探索先。None なら ``~/.grok/installed-plugins``。

    Returns:
        launcher.py を含む最新ディレクトリ。無ければ None。

    Raises:
        例外は発生しません。
    """
    root = installed_dir if installed_dir is not None else _home() / ".grok" / "installed-plugins"
    if not root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        for path in root.iterdir():
            if not path.is_dir():
                continue
            if not path.name.startswith("bluecore-"):
                continue
            launcher = path / "src" / "bluecore" / "launcher.py"
            if launcher.is_file():
                candidates.append(path)
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def ensure_grok_plugin_root_symlink(
    *,
    plugin_root: Path | str | None = None,
    link_path: Path | str | None = None,
) -> Path | None:
    """``~/.grok/plugins/bluecore`` を実インストール先へ symlink する。

    Args:
        plugin_root: リンク先。None なら installed-plugins の最新 bluecore-*、
            または環境の CLAUDE_PLUGIN_ROOT が installed-plugins 配下ならそれ。
        link_path: リンクのパス。None なら ``~/.grok/plugins/bluecore``。

    Returns:
        作成・更新したリンク先 Path。何もしなければ None。

    Raises:
        例外は発生しません（OSError は握りつぶす）。
    """
    link = Path(link_path) if link_path is not None else _home() / ".grok" / "plugins" / "bluecore"

    target: Path | None = None
    if plugin_root is not None:
        candidate = Path(plugin_root)
        if (candidate / "src" / "bluecore" / "launcher.py").is_file():
            target = candidate.resolve()
        else:
            # 明示指定が無効ならフォールバックせず失敗（テスト・誤設定を隠さない）
            return None
    if target is None:
        env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
        if env_root and "/.grok/installed-plugins/" in env_root.replace("\\", "/"):
            candidate = Path(env_root)
            if (candidate / "src" / "bluecore" / "launcher.py").is_file():
                target = candidate.resolve()
    if target is None:
        target = find_latest_installed_bluecore()
        if target is not None:
            target = target.resolve()
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
            if (link / "src" / "bluecore" / "launcher.py").is_file():
                return None
            if link.is_dir():
                # 壊れた/空のディレクトリは置き換えない（安全側）— ファイルが無い時のみ
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
