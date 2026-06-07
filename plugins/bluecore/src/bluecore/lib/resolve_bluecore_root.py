"""
bluecore ソースルートの場所を解決します。
環境変数、標準インストール先、プラグインキャッシュを順に探索します。
テスト用の上書き引数も受け付けます。
"""

from __future__ import annotations

import os
from pathlib import Path

from bluecore.lib.constants import PLUGIN_NAME


def _resolve_env_root(env_root: str | None) -> Path | None:
    """環境変数 CLAUDE_PLUGIN_ROOT からルートパスを解決する。見つからなければ None。"""
    raw = env_root if env_root is not None else os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if raw and raw.strip():
        return Path(raw.strip())
    return None


def _contains_probe(root: Path, probe_paths: list[str]) -> bool:
    """候補ルートに探査対象ファイルのいずれかが存在するか確認する。"""
    return any((root / p).exists() for p in probe_paths)


def _search_plugin_cache(claude_dir: Path, probe_paths: list[str]) -> Path | None:
    """プラグインキャッシュ配下を走査し、probe ファイルを含む最初のバージョンディレクトリを返す。"""
    try:
        cache_base = claude_dir / "plugins" / "cache" / PLUGIN_NAME
        if not cache_base.exists():
            return None
        for org_entry in cache_base.iterdir():
            if not org_entry.is_dir():
                continue
            try:
                for ver_entry in org_entry.iterdir():
                    if not ver_entry.is_dir():
                        continue
                    if _contains_probe(ver_entry, probe_paths):
                        return ver_entry
            except OSError:
                continue
    except OSError:
        pass
    return None


def resolve_bluecore_root(
    *,
    home_dir: str | Path | None = None,
    env_root: str | None = None,
    probe: str | None = None,
) -> Path:
    """bluecore ソースルートディレクトリを解決する。

    Args:
        home_dir: 探索の起点となるホームディレクトリ（省略時は Path.home()）。
        env_root: 環境変数 CLAUDE_PLUGIN_ROOT の上書き値（テスト用）。
        probe: 存在確認に使う相対パス（省略時は core_utils.py）。

    Returns:
        解決した bluecore ソースルートの Path。
    """
    env_path = _resolve_env_root(env_root)
    if env_path:
        return env_path

    home = Path(home_dir) if home_dir else Path.home()
    claude_dir = home / ".claude"
    probe_paths = [probe] if probe else ["src/bluecore/lib/core_utils.py"]

    if _contains_probe(claude_dir, probe_paths):
        return claude_dir

    cached = _search_plugin_cache(claude_dir, probe_paths)
    if cached:
        return cached

    return claude_dir


__all__ = ["resolve_bluecore_root"]
