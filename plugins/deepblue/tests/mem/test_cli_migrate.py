"""cli migrate-settings コマンドのテスト。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _make_settings(tmp_path: Path, postgres_url: str = "") -> MagicMock:
    """settings.json と Settings モックをセットアップする。"""
    settings_dir = tmp_path / ".devgear"
    settings_dir.mkdir()
    settings_path = settings_dir / "settings.json"
    data = {"mem": {"sync": {"enabled": True, "postgres_url": postgres_url}}}
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    settings_path.chmod(0o600)
    s = MagicMock()
    return s


def _call_migrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """migrate-settings を HOME=tmp_path 配下で呼び出す。"""
    monkeypatch.setenv("HOME", str(tmp_path))
    from devgear.mem.cli import _handle_migrate_settings

    _handle_migrate_settings(MagicMock())


class TestMigrateSettingsNoUrl:
    """postgres_url 未設定時はスキップ。"""

    def test_no_settings_file_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """settings.json が存在しない場合は何もしない。"""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        from devgear.mem.cli import _handle_migrate_settings

        _handle_migrate_settings(MagicMock())  # 例外が出なければ OK

    def test_empty_url_is_noop(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """postgres_url が空の場合は settings.json を変更しない。"""
        _make_settings(tmp_path, "")
        settings_path = tmp_path / ".devgear" / "settings.json"
        before = settings_path.read_text()
        _call_migrate(tmp_path, monkeypatch)
        assert settings_path.read_text() == before


class TestMigrateSettingsPassword:
    """パスワード付き URL の分離テスト。"""

    def test_password_moved_to_pgpass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """パスワード入り URL → ~/.pgpass に書き出し、settings.json からパスワード除去。"""
        _make_settings(tmp_path, "postgresql://user:secret@host/db")
        _call_migrate(tmp_path, monkeypatch)

        settings_path = tmp_path / ".devgear" / "settings.json"
        data = json.loads(settings_path.read_text())
        url = data["mem"]["sync"]["postgres_url"]
        assert "secret" not in url
        assert "user" in url

        pgpass_path = tmp_path / ".pgpass"
        assert pgpass_path.exists()
        assert "secret" in pgpass_path.read_text()

    def test_pgpass_chmod_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """移行後の ~/.pgpass は chmod 0600。"""
        _make_settings(tmp_path, "postgresql://user:pass@host/db")
        _call_migrate(tmp_path, monkeypatch)
        pgpass_path = tmp_path / ".pgpass"
        assert pgpass_path.stat().st_mode & 0o777 == 0o600

    def test_settings_json_chmod_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """移行後の settings.json は chmod 0600。"""
        _make_settings(tmp_path, "postgresql://user:pass@host/db")
        _call_migrate(tmp_path, monkeypatch)
        settings_path = tmp_path / ".devgear" / "settings.json"
        assert settings_path.stat().st_mode & 0o777 == 0o600

    def test_bak_file_created(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """移行時にバックアップファイルが作成される。"""
        _make_settings(tmp_path, "postgresql://user:pass@host/db")
        _call_migrate(tmp_path, monkeypatch)
        bak_files = list((tmp_path / ".devgear").glob("settings.json.bak-*"))
        assert len(bak_files) == 1

    def test_idempotent_no_double_pgpass(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """2回実行しても ~/.pgpass に重複エントリが追加されない。"""
        _make_settings(tmp_path, "postgresql://user:pass@host/db")
        _call_migrate(tmp_path, monkeypatch)
        # 1回目で URL からパスワードが除去されるため 2回目は noop
        _call_migrate(tmp_path, monkeypatch)
        content = (tmp_path / ".pgpass").read_text()
        # エントリが 1 件のみ
        lines = [ln for ln in content.splitlines() if ln.strip()]
        assert len(lines) == 1


class TestMigrateSettingsSslmode:
    """sslmode 正規化テスト。"""

    def test_no_sslmode_adds_require(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sslmode 未指定 URL には sslmode=require が付与される。"""
        _make_settings(tmp_path, "postgresql://user@host/db")
        _call_migrate(tmp_path, monkeypatch)
        data = json.loads((tmp_path / ".devgear" / "settings.json").read_text())
        assert "sslmode=require" in data["mem"]["sync"]["postgres_url"]

    def test_disable_replaced_with_require(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sslmode=disable → sslmode=require に書き換えられる。"""
        _make_settings(tmp_path, "postgresql://user@host/db?sslmode=disable")
        _call_migrate(tmp_path, monkeypatch)
        data = json.loads((tmp_path / ".devgear" / "settings.json").read_text())
        url = data["mem"]["sync"]["postgres_url"]
        assert "sslmode=disable" not in url
        assert "sslmode=require" in url

    def test_require_unchanged(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """sslmode=require はそのまま維持される（ファイルも変更されない）。"""
        _make_settings(tmp_path, "postgresql://user@host/db?sslmode=require")
        settings_path = tmp_path / ".devgear" / "settings.json"
        before = settings_path.read_text()
        _call_migrate(tmp_path, monkeypatch)
        # 変更なしのため bak が作成されないことを確認
        bak_files = list((tmp_path / ".devgear").glob("settings.json.bak-*"))
        assert not bak_files
        assert settings_path.read_text() == before
