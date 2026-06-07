"""
プロジェクトのルート探索とフォーマッタ解決をまとめる。
Biome と Prettier の検出優先度を管理し、ローカルインストールがあればそれを優先する。
パッケージマネージャー由来のランナー情報もこのモジュールで統一する。
"""

from __future__ import annotations

import json
import platform
import threading
from pathlib import Path
from typing import NamedTuple

# ── 設定ファイル一覧（単一の正解元） ─────────────────────

BIOME_CONFIGS = ["biome.json", "biome.jsonc"]

PRETTIER_CONFIGS = [
    ".prettierrc",
    ".prettierrc.json",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.mjs",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    ".prettierrc.toml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
]

PROJECT_ROOT_MARKERS = ["package.json", *BIOME_CONFIGS, *PRETTIER_CONFIGS]

# ── Windows .cmd シム対応表 ───────────────────────────────────────
WIN_CMD_SHIMS = {
    "npx": "npx.cmd",
    "pnpm": "pnpm.cmd",
    "yarn": "yarn.cmd",
    "bunx": "bunx.cmd",
}

# ── フォーマッタ → パッケージ名対応表 ────────────────────────────────
FORMATTER_PACKAGES = {
    "biome": {"bin_name": "biome", "pkg_name": "@biomejs/biome"},
    "prettier": {"bin_name": "prettier", "pkg_name": "prettier"},
}


# ── キャッシュ ──────────────────────────────────────────────────────────
_project_root_cache: dict[str, str] = {}
_formatter_cache: dict[str, str | None] = {}
_bin_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


class RunnerInfo(NamedTuple):
    """ランナーのバイナリ情報。"""

    bin: str
    prefix: list[str]


class FormatterBinInfo(NamedTuple):
    """フォーマッタのバイナリ情報。"""

    bin: str
    prefix: list[str]


def find_project_root(start_dir: str | Path) -> str:
    """`start_dir` から上方向にたどり、既知のプロジェクトルートマーカーを含むディレクトリを探す。
    見つからない場合は `start_dir` をそのまま返す。

    Args:
        start_dir: start_dir の値

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    start_dir_str = str(start_dir)
    with _cache_lock:
        if start_dir_str in _project_root_cache:
            return _project_root_cache[start_dir_str]

    dir_path = Path(start_dir).resolve()
    while True:
        for marker in PROJECT_ROOT_MARKERS:
            if (dir_path / marker).exists():
                result = str(dir_path)
                with _cache_lock:
                    _project_root_cache.setdefault(start_dir_str, result)
                return result

        parent = dir_path.parent
        if parent == dir_path:
            # ファイルシステムのルートに到達した
            break
        dir_path = parent

    with _cache_lock:
        _project_root_cache.setdefault(start_dir_str, start_dir_str)
    return start_dir_str


def detect_formatter(project_root: str | Path) -> str | None:
    """プロジェクトで設定されているフォーマッタを検出する。
    Biome は Prettier より優先する。

    Args:
        project_root: プロジェクトルート

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    project_root_str = str(project_root)
    with _cache_lock:
        if project_root_str in _formatter_cache:
            return _formatter_cache[project_root_str]

    root_path = Path(project_root)

    # Biome 設定を先に確認する（優先度が高い）
    for cfg in BIOME_CONFIGS:
        if (root_path / cfg).exists():
            with _cache_lock:
                _formatter_cache.setdefault(project_root_str, "biome")
            return "biome"

    # 設定ファイルより前に package.json の "prettier" キーを確認する
    try:
        pkg_path = root_path / "package.json"
        if pkg_path.exists():
            pkg = json.loads(pkg_path.read_text(encoding="utf-8"))
            if "prettier" in pkg:
                with _cache_lock:
                    _formatter_cache.setdefault(project_root_str, "prettier")
                return "prettier"
    except (json.JSONDecodeError, OSError):
        # package.json が壊れていても、ファイルベースの検出を続ける
        pass

    # Prettier の設定ファイルを確認する
    for cfg in PRETTIER_CONFIGS:
        if (root_path / cfg).exists():
            with _cache_lock:
                _formatter_cache.setdefault(project_root_str, "prettier")
            return "prettier"

    with _cache_lock:
        _formatter_cache.setdefault(project_root_str, None)
    return None


def get_runner_from_package_manager(project_root: str | Path) -> RunnerInfo:
    """設定済みのパッケージマネージャーに応じたランナーのバイナリとプレフィックス引数を解決する。
    CLAUDE_PACKAGE_MANAGER 環境変数とプロジェクト設定を考慮する。

    Args:
        project_root: プロジェクトルート

    Returns:
        RunnerInfo: 取得結果を返します。

    Raises:
        例外は発生しません。
    """
    from bluecore.lib.package_manager import get_package_manager

    is_win = platform.system() == "Windows"
    pm = get_package_manager(project_dir=str(project_root))
    # PM が未検出（Node.js 以外）の場合は npx をフォールバックとして使用
    exec_cmd = pm.config.exec_cmd if pm.config is not None else "npx"

    parts = exec_cmd.split()
    raw_bin = parts[0] if parts else "npx"
    prefix = parts[1:] if len(parts) > 1 else []

    bin_name = WIN_CMD_SHIMS.get(raw_bin, raw_bin) if is_win else raw_bin
    return RunnerInfo(bin=bin_name, prefix=prefix)


def resolve_formatter_bin(
    project_root: str | Path,
    formatter: str,
) -> FormatterBinInfo | None:
    """フォーマッタのバイナリを解決する。
    パッケージ解決のオーバーヘッドを避けるため、ローカルの node_modules/.bin を優先する。

    Args:
        project_root: プロジェクトルート
        formatter: formatter の値

    Returns:
        FormatterBinInfo | None: FormatterBinInfo を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    project_root_str = str(project_root)
    cache_key = f"{project_root_str}:{formatter}"
    with _cache_lock:
        if cache_key in _bin_cache:
            cached = _bin_cache[cache_key]
            return FormatterBinInfo(**cached) if cached else None

    pkg = FORMATTER_PACKAGES.get(formatter)
    if not pkg:
        with _cache_lock:
            _bin_cache.setdefault(cache_key, None)
        return None

    is_win = platform.system() == "Windows"
    bin_name = f"{pkg['bin_name']}.cmd" if is_win else pkg["bin_name"]
    local_bin = Path(project_root) / "node_modules" / ".bin" / bin_name

    if local_bin.exists():
        result = FormatterBinInfo(bin=str(local_bin), prefix=[])
        with _cache_lock:
            _bin_cache.setdefault(cache_key, {"bin": result.bin, "prefix": result.prefix})
        return result

    runner = get_runner_from_package_manager(project_root)
    result = FormatterBinInfo(bin=runner.bin, prefix=[*runner.prefix, pkg["pkg_name"]])
    with _cache_lock:
        _bin_cache.setdefault(cache_key, {"bin": result.bin, "prefix": result.prefix})
    return result


def clear_caches() -> None:
    """すべてのキャッシュをクリアする。
    テスト時にルート探索やフォーマッタ検出の結果をリセットしたい場合に使う。

    Returns:
        None: 値を返しません。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    with _cache_lock:
        _project_root_cache.clear()
        _formatter_cache.clear()
        _bin_cache.clear()


__all__ = [
    "BIOME_CONFIGS",
    "FORMATTER_PACKAGES",
    "PRETTIER_CONFIGS",
    "PROJECT_ROOT_MARKERS",
    "WIN_CMD_SHIMS",
    "FormatterBinInfo",
    "RunnerInfo",
    "clear_caches",
    "detect_formatter",
    "find_project_root",
    "get_runner_from_package_manager",
    "resolve_formatter_bin",
]
