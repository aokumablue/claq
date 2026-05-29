"""settings.py の SlimSettings に関するテスト。

slim はハードコード既定値のみで、settings.json に永続化されないことを検証する。
"""

from __future__ import annotations

import json

import pytest

from devgear.mem.settings import Settings, SlimSettings


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """各テストで Settings の保存先を一時ディレクトリに固定する。"""
    import devgear.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


class TestSlimSettingsDefaults:
    """SlimSettings デフォルト値のテスト"""

    def test_default_enabled(self) -> None:
        """enabled のデフォルトは True。"""
        s = SlimSettings()
        assert s.enabled is True

    def test_no_default_mode_field(self) -> None:
        """default_mode フィールドは存在しない。"""
        s = SlimSettings()
        assert not hasattr(s, "default_mode")


class TestSlimNotPersisted:
    """slim セクションは settings.json に書き出されない。"""

    def test_save_does_not_write_slim_section(self, tmp_path) -> None:
        path = tmp_path / "settings.json"
        settings = Settings()
        settings.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert "slim" not in raw["mem"]

    def test_load_ignores_legacy_slim_section(self, tmp_path) -> None:
        """旧 settings.json に slim セクションがあっても読み込まれず、既定値が使われる。"""
        path = tmp_path / "settings.json"
        raw = {"mem": {"slim": {"enabled": False}, "sync": {"enabled": False, "postgres_url": ""}}}
        path.write_text(json.dumps(raw), encoding="utf-8")

        loaded = Settings.load(settings_path=path)
        # slim セクションは無視されるのでデフォルト True
        assert loaded.slim.enabled is True

    def test_other_sections_preserved_after_save(self, tmp_path) -> None:
        """save 後に他セクション（flow 等）が保持される。"""
        path = tmp_path / "settings.json"
        raw = {"flow": {"key": "value"}, "mem": {}}
        path.write_text(json.dumps(raw), encoding="utf-8")

        settings = Settings.load(settings_path=path)
        settings.save()

        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved.get("flow", {}).get("key") == "value"
