"""settings のテスト。

Settings はハードコード既定値のみを持つランタイム設定であり、
永続化するランタイム状態を持たないことを検証する。
"""

import importlib
from pathlib import Path

import pytest

from bluecore.lib.constants import BASE_DIR_NAME
from bluecore.mem.settings import Settings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで ~/.bluecore の代わりに一時ディレクトリを使う。"""
    import bluecore.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


class TestSettingsDefaults:
    """デフォルト値のテスト"""

    def test_default_values(self) -> None:
        s = Settings()
        assert s.log_level == "info"

    def test_no_warm_settings(self) -> None:
        """warm 層設定（廃止済み）が存在しないことを確認する。"""
        s = Settings()
        assert not hasattr(s, "context_warm_tokens")
        assert not hasattr(s, "context_warm_days")

    def test_no_context_max_tokens(self) -> None:
        """context_max_tokens（死コード化していたため削除済み）が存在しないことを確認する。"""
        s = Settings()
        assert not hasattr(s, "context_max_tokens")

    def test_no_sync_or_team_settings(self) -> None:
        """PostgreSQL チーム同期関連の設定（廃止済み）が存在しないことを確認する。"""
        s = Settings()
        assert not hasattr(s, "sync")
        assert not hasattr(s, "team")

    def test_no_chunk_or_auto_compact_settings(self) -> None:
        """チャンク・自動圧縮関連の設定（死コード化のため削除済み）が存在しないことを確認する。"""
        s = Settings()
        for name in (
            "excluded_projects",
            "search_half_life_days",
            "chunk_max_length",
            "context_chunk_count",
            "context_hot_tokens",
            "context_hot_hours",
            "context_digest_tokens",
            "context_digest_count",
            "auto_compact_enabled",
            "auto_compact_interval_days",
            "last_compacted_at",
            "embedding_model",
        ):
            assert not hasattr(s, name), name

    def test_no_state_persistence(self) -> None:
        """state ファイルの読み書き API（削除済み）が存在しないことを確認する。"""
        assert not hasattr(Settings, "save")
        assert not hasattr(Settings, "load")
        assert not hasattr(Settings(), "state_path")

    def test_derived_properties(self, tmp_path: Path) -> None:
        s = Settings()
        assert s.data_path == tmp_path
        assert s.db_path == tmp_path / "mem.db"
        assert s.log_dir == tmp_path / "logs"

    def test_construction_does_not_touch_disk(self, tmp_path: Path) -> None:
        """Settings の構築はファイルを一切作らない。"""
        Settings()
        assert list(tmp_path.iterdir()) == []


def test_default_data_dir_uses_bluecore_data_path_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """BLUECORE_DATA_PATH があればモジュール再読込時にそれをデータディレクトリにする。"""
    import bluecore.mem.settings as settings_mod

    custom = tmp_path / "custom-data"
    monkeypatch.setenv("BLUECORE_DATA_PATH", str(custom))
    importlib.reload(settings_mod)
    try:
        assert settings_mod._DEFAULT_DATA_DIR == custom
        assert settings_mod.Settings().data_path == custom
    finally:
        monkeypatch.delenv("BLUECORE_DATA_PATH", raising=False)
        importlib.reload(settings_mod)
        assert settings_mod._DEFAULT_DATA_DIR == Path.home() / BASE_DIR_NAME
