"""settings のテスト。

Settings はハードコード既定値のみを持つランタイム設定であり、
永続化するランタイム状態を持たないことを検証する。
"""

import importlib
from pathlib import Path

import pytest

from claq.lib.constants import BASE_DIR_NAME
from claq.mem.settings import Settings

#: 削除済みの設定項目と state 永続化 API。どれも再導入しない。
_REMOVED_ATTRIBUTES = (
    *("context_warm_tokens", "context_warm_days", "context_max_tokens"),  # warm 層と旧トークン上限
    *("sync", "team"),  # PostgreSQL チーム同期
    # チャンク・自動圧縮（死コード化のため削除済み）
    *("excluded_projects", "search_half_life_days", "chunk_max_length", "context_chunk_count"),
    *("context_hot_tokens", "context_hot_hours", "context_digest_tokens", "context_digest_count"),
    *("auto_compact_enabled", "auto_compact_interval_days", "last_compacted_at", "embedding_model"),
    *("save", "load", "state_path"),  # state ファイルの読み書き
)


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで ~/.claq の代わりに一時ディレクトリを使う。"""
    import claq.mem.settings as mod

    monkeypatch.setattr(mod, "_default_data_dir", lambda: tmp_path)


def test_defaults_and_derived_paths_without_touching_disk(tmp_path: Path) -> None:
    """既定値と導出パスを返し、構築してもファイルを 1 つも作らない。"""
    s = Settings()

    assert (s.log_level, s.data_path, s.db_path, s.log_dir) == (
        "info",
        tmp_path,
        tmp_path / "mem.db",
        tmp_path / "logs",
    )
    assert list(tmp_path.iterdir()) == []


def test_removed_settings_and_state_api_stay_absent() -> None:
    """削除済みの設定項目・state 永続化 API が Settings に存在しない（インスタンス経由でクラス属性も見る）。"""
    s = Settings()
    for name in _REMOVED_ATTRIBUTES:
        assert not hasattr(s, name), name


def test_default_data_dir_uses_claq_data_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """CLAQ_DATA_PATH があればモジュール再読込時にそれをデータディレクトリにする。"""
    import claq.mem.settings as settings_mod

    custom = tmp_path / "custom-data"
    monkeypatch.setenv("CLAQ_DATA_PATH", str(custom))
    importlib.reload(settings_mod)
    try:
        assert settings_mod._default_data_dir() == custom
        assert settings_mod.Settings().data_path == custom
    finally:
        monkeypatch.delenv("CLAQ_DATA_PATH", raising=False)
        importlib.reload(settings_mod)
        assert settings_mod._default_data_dir() == Path.home() / BASE_DIR_NAME
