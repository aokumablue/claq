"""devgear ルート解決ロジックのテスト。

環境変数、標準インストール場所、Git リポジトリ構造からの
プラグインルートディレクトリ検出を対象とする。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.lib.resolve_devgear_root import resolve_devgear_root


def _write_probe(root: Path, probe: str = "src/devgear/lib/core_utils.py") -> Path:
    probe_path = root / probe
    probe_path.parent.mkdir(parents=True, exist_ok=True)
    probe_path.write_text("# probe\n", encoding="utf-8")
    return probe_path


def test_resolve_devgear_root_uses_env_root() -> None:
    assert resolve_devgear_root(env_root="  /custom/root  ") == Path("/custom/root")


def test_resolve_devgear_root_prefers_claude_plugin_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "  /claude/root  ")

    assert resolve_devgear_root(home_dir=Path("/unused"), env_root=None) == Path("/claude/root")


def test_resolve_devgear_root_prefers_standard_install(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    _write_probe(claude_dir)

    assert resolve_devgear_root(home_dir=tmp_path, env_root="") == claude_dir


def test_resolve_devgear_root_finds_plugin_cache(tmp_path: Path) -> None:
    expected = tmp_path / ".claude" / "plugins" / "cache" / "devgear" / "org" / "0.0.1"
    _write_probe(expected)

    assert resolve_devgear_root(home_dir=tmp_path, env_root="") == expected


def test_resolve_devgear_root_falls_back_to_claude_dir(tmp_path: Path) -> None:
    assert resolve_devgear_root(home_dir=tmp_path, env_root="") == tmp_path / ".claude"


def test_resolve_devgear_root_supports_custom_probe(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    _write_probe(claude_dir, "custom/marker.js")

    assert resolve_devgear_root(home_dir=tmp_path, env_root="", probe="custom/marker.js") == claude_dir


def test_resolve_devgear_root_skips_non_dir_org_entries(tmp_path: Path) -> None:
    """org エントリがファイル（dir でない）の場合はスキップされ、fallback を返す。"""
    cache_base = tmp_path / ".claude" / "plugins" / "cache" / "devgear"
    cache_base.mkdir(parents=True)
    # ディレクトリではなくファイルを配置
    (cache_base / "not_a_dir.txt").write_text("file", encoding="utf-8")

    result = resolve_devgear_root(home_dir=tmp_path, env_root="")
    assert result == tmp_path / ".claude"


def test_resolve_devgear_root_skips_non_dir_version_entries(tmp_path: Path) -> None:
    """バージョンエントリがファイル（dir でない）の場合はスキップされる。"""
    org_dir = tmp_path / ".claude" / "plugins" / "cache" / "devgear" / "org"
    org_dir.mkdir(parents=True)
    # バージョンエントリとしてファイルを配置
    (org_dir / "0.0.1.tar.gz").write_text("archive", encoding="utf-8")

    result = resolve_devgear_root(home_dir=tmp_path, env_root="")
    assert result == tmp_path / ".claude"


def test_resolve_devgear_root_handles_inner_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """内部 iterdir（バージョン列挙）が OSError を起こした場合も fallback を返す。"""
    from pathlib import Path as _Path

    org_dir = tmp_path / ".claude" / "plugins" / "cache" / "devgear" / "org"
    org_dir.mkdir(parents=True)
    ver_dir = org_dir / "0.0.1"
    ver_dir.mkdir()

    call_count = {"n": 0}
    original_iterdir = _Path.iterdir

    def _patched_iterdir(self: _Path):
        call_count["n"] += 1
        # 2回目 (ver_dir のイテレート) だけ OSError を発生させる
        if call_count["n"] == 2:
            raise OSError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(_Path, "iterdir", _patched_iterdir)

    result = resolve_devgear_root(home_dir=tmp_path, env_root="")
    assert result == tmp_path / ".claude"


def test_resolve_devgear_root_handles_outer_oserror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外側 iterdir（org 列挙）が OSError を起こした場合も fallback を返す。"""
    from pathlib import Path as _Path

    cache_base = tmp_path / ".claude" / "plugins" / "cache" / "devgear"
    cache_base.mkdir(parents=True)

    original_iterdir = _Path.iterdir

    def _patched_iterdir(self: _Path):
        if str(self) == str(cache_base):
            raise OSError("permission denied")
        return original_iterdir(self)

    monkeypatch.setattr(_Path, "iterdir", _patched_iterdir)

    result = resolve_devgear_root(home_dir=tmp_path, env_root="")
    assert result == tmp_path / ".claude"
