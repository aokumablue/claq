"""Grok Build のプラグインルートと hooks が期待するパスを橋渡しする。

Grok は ``${CLAUDE_PLUGIN_ROOT}`` を ``~/.grok/plugins/<name>`` に展開するが、
実体は ``~/.grok/installed-plugins/<name>-<hash>/`` に置かれる。hooks.json の
launcher パスが前者を参照するため、両者を一致させるにはシンボリックリンクが
必要になる。SessionStart からの自動修復は行わない（同一マシンに Claude/
Copilot と Grok が同居する環境で、Claude 側セッション開始のたびに Grok 側の
symlink を無断で書き換える副作用があるため）。ユーザーが
``scripts/grok.sh`` を手動実行したときに、本モジュールの
``ensure_grok_plugin_root_symlink`` が呼ばれてリンクを張る。

リンク先は常に ``~/.grok/installed-plugins/claq-*`` のみとする。
開発用チェックアウト（例: claq-dev）は一切参照しない。
"""

from __future__ import annotations

import os
from pathlib import Path


def installed_plugins_dir(home: Path | None = None) -> Path:
    """``~/.grok/installed-plugins`` のパスを返す（``home`` は None なら ``Path.home()``）。"""
    base = home if home is not None else Path.home()
    return base / ".grok" / "installed-plugins"


def is_grok_installed_plugin_root(path: Path | str) -> bool:
    """パスが Grok の公式インストール先（installed-plugins/claq-* 配下）か判定する。"""
    try:
        parts = Path(path).resolve().parts
    except OSError:
        return False
    try:
        grok_idx = parts.index(".grok")
    except ValueError:
        return False
    if grok_idx + 2 >= len(parts) or parts[grok_idx + 1] != "installed-plugins":
        return False
    return parts[grok_idx + 2].startswith("claq-")


def _has_launcher(root: Path) -> bool:
    """プラグインルートに launcher.py があるか。"""
    return (root / "src" / "claq" / "launcher.py").is_file()


def _safe_resolve(path: Path) -> Path | None:
    """``path.resolve()`` を試み、失敗したら None を返す。"""
    try:
        return path.resolve()
    except OSError:
        return None


def _installed_target(path: Path | str) -> Path | None:
    """installed-plugins/claq-* かつ launcher があるなら resolve した Path。"""
    candidate = Path(path)
    if not is_grok_installed_plugin_root(candidate) or not _has_launcher(candidate):
        return None
    return _safe_resolve(candidate)


def _mtime_or_none(path: Path) -> float | None:
    """``path`` の mtime を返し、stat に失敗したら None を返す。"""
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def find_latest_installed_claq(installed_dir: Path | None = None) -> Path | None:
    """``~/.grok/installed-plugins/claq-*`` のうち launcher.py を含む最新のディレクトリを返す。

    mtime の取得に失敗した候補は除外する。列挙と stat の間には隙間があり（TOCTOU）、
    Grok が同時に古い ``claq-<hash>`` を消すと ``FileNotFoundError`` が起きる。stat を
    try の外で行うと、それが ``ensure_grok_plugin_root_symlink`` まで貫通し、
    ``scripts/grok.sh`` が traceback で異常終了する。

    Args:
        installed_dir: 探索先。None なら ``~/.grok/installed-plugins``。

    Returns:
        最新ディレクトリ。無ければ None（全候補の stat が失敗した場合も None）。

    Raises:
        例外は発生しません。
    """
    root = installed_dir if installed_dir is not None else installed_plugins_dir()
    if not root.is_dir():
        return None
    candidates: list[tuple[float, Path]] = []
    try:
        for path in root.iterdir():
            if not (path.is_dir() and path.name.startswith("claq-") and _has_launcher(path)):
                continue
            mtime = _mtime_or_none(path)
            if mtime is not None:
                candidates.append((mtime, path))
    except OSError:
        return None
    if not candidates:
        return None
    # 同 mtime のときは iterdir の順を保つため max の「最初の最大要素を返す」性質に
    # 委ね、Path 同士を比較させない。
    return max(candidates, key=lambda item: item[0])[1]


def ensure_grok_plugin_root_symlink(
    *,
    plugin_root: Path | str | None = None,
    link_path: Path | str | None = None,
    home: Path | None = None,
) -> Path | None:
    """``~/.grok/plugins/claq`` を **installed-plugins 配下のみ** へ symlink する。

    開発用リポジトリや任意パスはリンク先にしない。明示の ``plugin_root`` も
    ``is_grok_installed_plugin_root`` を満たす場合だけ採用する。

    Args:
        plugin_root: 候補のリンク先。installed-plugins/claq-* でなければ無視。
        link_path: リンクのパス。None なら ``~/.grok/plugins/claq``。
        home: ホーム上書き（テスト用）。

    Returns:
        作成・更新したリンク先 Path。何もしなければ None（OSError は握りつぶす）。
    """
    base = home if home is not None else Path.home()
    link = Path(link_path) if link_path is not None else base / ".grok" / "plugins" / "claq"

    target = _resolve_symlink_target(plugin_root, base)
    if target is None:
        return None
    return _ensure_symlink(link, target)


def _resolve_symlink_target(plugin_root: Path | str | None, home: Path) -> Path | None:
    """リンク先を plugin_root → 環境変数 → 最新インストールの順で解決する。"""
    env_root = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    for candidate in (plugin_root, env_root or None):
        if candidate is not None and (target := _installed_target(candidate)) is not None:
            return target
    found = find_latest_installed_claq(installed_plugins_dir(home))
    if found is None:
        return None
    return _safe_resolve(found)


def _remove_empty_or_file(link: Path) -> bool:
    """空ディレクトリまたは通常ファイルを削除する。触ってはいけないなら False。"""
    if not link.is_dir():
        link.unlink()
        return True
    try:
        next(link.iterdir())
    except StopIteration:
        link.rmdir()
        return True
    except OSError:
        return False
    return False


def _is_link_to(link: Path, target: Path) -> bool:
    """``link`` が ``target`` へ解決されるシンボリックリンクか。resolve が OSError なら False。"""
    try:
        return link.resolve() == target
    except OSError:
        return False


def _clear_existing(link: Path) -> bool:
    """``link`` の既存物を片付ける。触ってはいけない（launcher を含む / 空でないディレクトリ）なら False。"""
    if link.is_symlink():
        link.unlink()
        return True
    if link.exists():
        return not _has_launcher(link) and _remove_empty_or_file(link)
    return True


def _ensure_symlink(link: Path, target: Path) -> Path | None:
    """``link`` を ``target`` へ張り、結果の Path または何もしない/失敗時の None を返す。"""
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.is_symlink() and _is_link_to(link, target):
            return target
        if not _clear_existing(link):
            return None
        link.symlink_to(target, target_is_directory=True)
        return target
    except OSError:
        return None
