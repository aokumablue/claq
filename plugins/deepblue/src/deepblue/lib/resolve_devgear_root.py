"""
devgear ソースルートの場所を解決します。
環境変数、標準インストール先、プラグインキャッシュを順に探索します。
テスト用の上書き引数も受け付けます。
"""

from __future__ import annotations

import os
from pathlib import Path


def resolve_devgear_root(
    *,
    home_dir: str | Path | None = None,
    env_root: str | None = None,
    probe: str | None = None,
) -> Path:
    """devgear ソースルートディレクトリを解決する。

    Args:
        home_dir: ホームディレクトリ
        env_root: env_root の値
        probe: probe の値

    Returns:
        Path: 解決結果を返します。

    Raises:
        例外は発生しません。
    """
    # 環境変数を確認する（CLAUDE_PLUGIN_ROOT を優先）
    if env_root is not None:
        root_from_env = env_root
    else:
        root_from_env = os.environ.get("CLAUDE_PLUGIN_ROOT", "")
    if root_from_env and root_from_env.strip():
        return Path(root_from_env.strip())

    # home ディレクトリと claude ディレクトリを決定する
    home = Path(home_dir) if home_dir else Path.home()
    claude_dir = home / ".claude"

    probe_paths = [probe] if probe else ["src/devgear/lib/core_utils.py"]

    def _contains_probe(root: Path) -> bool:
        """候補ルートに探査対象ファイルが存在するか確認する。

        Args:
            root: 探索や判定の基点となるルートパス。

        Returns:
            True / False を返す真偽値。

        Raises:
            例外は発生しません。
        """
        return any((root / probe_path).exists() for probe_path in probe_paths)

    # 標準インストール — ファイルは ~/.claude/ に直接コピーされる
    if _contains_probe(claude_dir):
        return claude_dir

    # プラグインキャッシュ — マーケットプレイスのプラグインを
    # ~/.claude/plugins/cache/<plugin-name>/<org>/<version>/ に配置
    try:
        cache_base = claude_dir / "plugins" / "cache" / "devgear"
        if cache_base.exists():
            for org_entry in cache_base.iterdir():
                if not org_entry.is_dir():
                    continue

                try:
                    for ver_entry in org_entry.iterdir():
                        if not ver_entry.is_dir():
                            continue
                        if _contains_probe(ver_entry):
                            return ver_entry
                except OSError:
                    continue
    except OSError:
        pass

    return claude_dir


__all__ = ["resolve_devgear_root"]
