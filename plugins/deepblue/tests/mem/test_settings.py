"""settings のテスト。

settings.json は ``mem.sync.enabled`` / ``mem.sync.postgres_url`` のみ永続化し、
ランタイム状態（last_synced_at など）は ``sync_state.json`` で管理する構成を検証する。
"""

import json
from pathlib import Path

import pytest

from devgear.mem.settings import (
    _DEFAULT_EMBEDDING_MODEL,
    Settings,
    _strip_password_to_pgpass,
)


@pytest.fixture(autouse=True)
def _patch_default_data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """各テストで ~/.devgear の代わりに一時ディレクトリを使う。"""
    import devgear.mem.settings as mod

    monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)


class TestSettingsDefaults:
    """デフォルト値のテスト"""

    def test_default_values(self) -> None:
        s = Settings()
        assert s.log_level == "info"
        assert s.embedding_model == _DEFAULT_EMBEDDING_MODEL
        assert s.search_half_life_days == 30.0
        assert s.chunk_max_length == 2000
        assert s.context_chunk_count == 30
        assert s.context_max_tokens == 1500
        assert s.context_hot_tokens == 400
        assert s.context_warm_tokens == 600
        assert s.excluded_projects == []

    def test_derived_properties(self, tmp_path: Path) -> None:
        s = Settings()
        assert s.data_path == tmp_path
        assert s.db_path == tmp_path / "mem.db"
        assert s.log_dir == tmp_path / "logs"
        assert s.settings_path == tmp_path / "settings.json"
        assert s.sync_state_path == tmp_path / "sync_state.json"


class TestSettingsSave:
    """永続化のテスト"""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        s = Settings()
        s.save()
        assert (tmp_path / "settings.json").exists()

    def test_save_only_writes_sync_section(self, tmp_path: Path) -> None:
        """save() は mem.sync.enabled と mem.sync.postgres_url のみを書き出す。"""
        s = Settings()
        s.sync.enabled = True
        s.sync.postgres_url = "postgres://example"
        s.save()
        raw = json.loads((tmp_path / "settings.json").read_text())
        assert raw["mem"] == {"sync": {"enabled": True, "postgres_url": "postgres://example"}}

    def test_save_does_not_write_hardcoded_fields(self, tmp_path: Path) -> None:
        """save() はハードコード対象（log_level 等）を書き出さない。"""
        s = Settings()
        s.save()
        raw = json.loads((tmp_path / "settings.json").read_text())
        assert "log_level" not in raw["mem"]
        assert "chunk_max_length" not in raw["mem"]
        assert "embedding_model" not in raw["mem"]
        assert "last_compacted_at" not in raw["mem"]
        assert "compact" not in raw["mem"]
        assert "slim" not in raw["mem"]

    def test_save_preserves_other_plugin_sections(self, tmp_path: Path) -> None:
        """保存時に他プラグインのセクションを保持する"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"flow": {"some_key": "some_value"}}))
        s = Settings()
        s.save()
        raw = json.loads(settings_file.read_text())
        assert raw["flow"]["some_key"] == "some_value"
        assert raw["mem"]["sync"]["enabled"] is False

    def test_save_reads_broken_existing_file_gracefully(self, tmp_path: Path) -> None:
        """既存ファイルが破損 JSON の場合、save() は空辞書から書き直す。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text("{{broken json")
        s = Settings()
        s.save()
        raw = json.loads(settings_file.read_text())
        assert "mem" in raw

    def test_save_reads_unreadable_existing_file_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """既存ファイルの read_text が OSError を起こした場合、空辞書から書き直す。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text("{}")

        call_count = {"n": 0}
        original_read_text = Path.read_text

        def _patched_read_text(self_path: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_path) == str(settings_file) and call_count["n"] == 0:
                call_count["n"] += 1
                raise OSError("permission denied")
            return original_read_text(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _patched_read_text)

        s = Settings()
        s.save()
        raw = json.loads(settings_file.read_text())
        assert "mem" in raw


class TestSettingsLoad:
    """読み込みのテスト"""

    def test_load_reads_only_sync_enabled_and_url(self, tmp_path: Path) -> None:
        """settings.json から sync.enabled / sync.postgres_url のみ反映する。

        settings.json に旧フィールド（log_level 等）が残っていても無視し、
        ハードコード既定値が使用される。
        """
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps(
                {
                    "mem": {
                        "log_level": "debug",
                        "chunk_max_length": 9999,
                        "sync": {
                            "enabled": True,
                            "postgres_url": "postgres://example",
                            # settings.json に残っていても読まれない
                            "last_synced_at": 123.0,
                            "last_sync_success": True,
                        },
                    }
                }
            )
        )
        s = Settings.load(settings_path=path)
        # settings.json の設定値は無視され、ハードコード既定値
        assert s.log_level == "info"
        assert s.chunk_max_length == 2000
        # sync.enabled / postgres_url のみ反映
        assert s.sync.enabled is True
        assert s.sync.postgres_url == "postgres://example"
        # settings.json の last_synced_at は無視される（ランタイム状態のため）
        assert s.sync.last_synced_at == 0.0
        assert s.sync.last_sync_success is False

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        s = Settings.load(settings_path=path)
        assert s.log_level == "info"
        assert s.sync.enabled is False

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("not json{{{")
        s = Settings.load(settings_path=path)
        assert s.log_level == "info"
        assert s.sync.enabled is False

    def test_load_invalid_json_with_default_path(self, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text("not json{{{")
        s = Settings.load()
        assert s.log_level == "info"
        assert s.sync.enabled is False

    def test_load_unknown_keys_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"mem": {"unknown_key": "val"}}))
        s = Settings.load(settings_path=path)
        assert not hasattr(s, "unknown_key")

    def test_load_default_creates_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """デフォルトパスで存在しない場合、ファイルを作成する"""
        import devgear.mem.settings as mod

        monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)
        Settings.load()
        assert (tmp_path / "settings.json").exists()

    def test_load_nonexistent_with_explicit_path_no_save(self, tmp_path: Path) -> None:
        """settings_path 指定時、存在しなければ save() しない"""
        path = tmp_path / "sub" / "settings.json"
        s = Settings.load(settings_path=path)
        assert not path.exists()
        assert s.log_level == "info"

    def test_load_sync_state_from_explicit_path(self, tmp_path: Path) -> None:
        """settings_path と同じディレクトリの sync_state.json があれば読み込む。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "u"}}}))
        (tmp_path / "sync_state.json").write_text(
            json.dumps(
                {
                    "last_synced_at": 42.0,
                    "last_sync_attempt_at": 41.0,
                    "last_sync_success": True,
                    "last_compacted_at": 100.0,
                }
            )
        )
        s = Settings.load(settings_path=settings_file)
        assert s.sync.last_synced_at == 42.0
        assert s.sync.last_sync_attempt_at == 41.0
        assert s.sync.last_sync_success is True
        assert s.last_compacted_at == 100.0

    def test_load_default_reads_sync_state(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """デフォルトパスで sync_state.json があれば自動で読み込む。"""
        import devgear.mem.settings as mod

        monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)
        (tmp_path / "sync_state.json").write_text(json.dumps({"last_compacted_at": 77.0}))

        s = Settings.load()
        assert s.last_compacted_at == 77.0

    def test_load_default_existing_settings_reads_sync_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """settings.json が既存でも、デフォルトパス経由で sync_state.json を読む。"""
        import devgear.mem.settings as mod

        monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)
        (tmp_path / "settings.json").write_text(
            json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "u"}}})
        )
        (tmp_path / "sync_state.json").write_text(json.dumps({"last_compacted_at": 12.0}))

        s = Settings.load()
        assert s.last_compacted_at == 12.0
        assert s.sync.enabled is True

    def test_load_broken_sync_state_is_ignored(self, tmp_path: Path) -> None:
        """sync_state.json が壊れていてもクラッシュしない。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"mem": {"sync": {"enabled": False, "postgres_url": ""}}}))
        (tmp_path / "sync_state.json").write_text("{{broken")
        s = Settings.load(settings_path=settings_file)
        assert s.last_compacted_at == 0.0

    def test_load_non_dict_sync_state_is_ignored(self, tmp_path: Path) -> None:
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "u"}}}))
        (tmp_path / "sync_state.json").write_text("[]")
        s = Settings.load(settings_path=settings_file)
        assert s.last_compacted_at == 0.0


class TestSaveSyncState:
    """ランタイム状態の永続化テスト"""

    def test_save_sync_state_creates_file(self, tmp_path: Path) -> None:
        s = Settings()
        s.sync.last_synced_at = 111.0
        s.sync.last_sync_attempt_at = 110.0
        s.sync.last_sync_success = True
        s.last_compacted_at = 222.0
        s.save_sync_state()

        raw = json.loads((tmp_path / "sync_state.json").read_text())
        assert raw == {
            "last_synced_at": 111.0,
            "last_sync_attempt_at": 110.0,
            "last_sync_success": True,
            "last_compacted_at": 222.0,
        }

    def test_save_sync_state_does_not_touch_settings_json(self, tmp_path: Path) -> None:
        """save_sync_state() は settings.json を書き換えない。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(json.dumps({"flow": {"a": 1}}))
        s = Settings()
        s.save_sync_state()
        # settings.json は変更されない
        assert json.loads(settings_file.read_text()) == {"flow": {"a": 1}}
        assert (tmp_path / "sync_state.json").exists()

    def test_sync_state_roundtrip(self, tmp_path: Path) -> None:
        """save_sync_state -> load で状態が復元される。"""
        settings_file = tmp_path / "settings.json"
        s = Settings()
        s.sync.enabled = True
        s.sync.postgres_url = "url"
        s.sync.last_synced_at = 1234567890.0
        s.last_compacted_at = 9999.0
        s.save()
        s.save_sync_state()

        loaded = Settings.load(settings_path=settings_file)
        assert loaded.sync.enabled is True
        assert loaded.sync.postgres_url == "url"
        assert loaded.sync.last_synced_at == 1234567890.0
        assert loaded.last_compacted_at == 9999.0


class TestAutoCompactSettings:
    """自動圧縮設定のテスト（ハードコード値）"""

    def test_auto_compact_defaults(self) -> None:
        s = Settings()
        assert s.auto_compact_enabled is True
        assert s.auto_compact_interval_days == 7
        assert s.last_compacted_at == 0.0

    def test_last_compacted_at_persisted_in_sync_state(self, tmp_path: Path) -> None:
        """last_compacted_at は sync_state.json に保存される。"""
        settings_file = tmp_path / "settings.json"
        s = Settings(last_compacted_at=1234567890.0)
        s.save()
        s.save_sync_state()

        loaded = Settings.load(settings_path=settings_file)
        assert loaded.last_compacted_at == 1234567890.0
        # auto_compact_enabled / interval_days はハードコード既定値
        assert loaded.auto_compact_enabled is True
        assert loaded.auto_compact_interval_days == 7


class TestSyncLockPathAndReload:
    """sync_lock_path プロパティと reload_sync_state のテスト。"""

    def test_sync_lock_path_is_under_data_dir(self, tmp_path: Path) -> None:
        s = Settings()
        assert s.sync_lock_path == tmp_path / "sync.lock"

    def test_reload_sync_state_refreshes_in_place(self, tmp_path: Path) -> None:
        """reload_sync_state は最新の sync_state.json を反映する。"""
        s = Settings()
        # 最初は空
        s.reload_sync_state()
        assert s.last_compacted_at == 0.0
        # 後から sync_state.json を作成して反映を確認
        (tmp_path / "sync_state.json").write_text(
            json.dumps({"last_compacted_at": 555.0, "last_sync_success": True})
        )
        s.reload_sync_state()
        assert s.last_compacted_at == 555.0
        assert s.sync.last_sync_success is True


class TestStripPasswordToPgpass:
    """_strip_password_to_pgpass のテスト。"""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """テスト中の HOME を一時ディレクトリに固定する。"""
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_returns_url_unchanged_when_no_password(self, tmp_path: Path) -> None:
        """パスワードがない URL はそのまま返す。"""
        url = "postgres://user@host:5432/db"
        assert _strip_password_to_pgpass(url) == url
        assert not (tmp_path / ".pgpass").exists()

    def test_strips_password_and_appends_pgpass(self, tmp_path: Path) -> None:
        """パスワード付き URL は ~/.pgpass に追記され、戻り値からは除去される。"""
        url = "postgres://alice:secret@db.example:5433/mydb"
        new_url = _strip_password_to_pgpass(url)
        assert "secret" not in new_url
        assert new_url == "postgres://alice@db.example:5433/mydb"
        pgpass = tmp_path / ".pgpass"
        assert pgpass.exists()
        contents = pgpass.read_text()
        assert "db.example:5433:mydb:alice:secret" in contents
        # パーミッションが 0600 に固定される
        mode = pgpass.stat().st_mode & 0o777
        assert mode == 0o600

    def test_does_not_append_duplicate_entry(self, tmp_path: Path) -> None:
        """同じ host:port:db:user の prefix を持つ行が既にあれば追記しない。"""
        pgpass = tmp_path / ".pgpass"
        pgpass.write_text("db.example:5433:mydb:alice:oldpw\n")
        url = "postgres://alice:newpw@db.example:5433/mydb"
        _strip_password_to_pgpass(url)
        contents = pgpass.read_text()
        # 既存行は維持され、追記されない
        assert contents.count("\n") == 1
        assert "oldpw" in contents
        assert "newpw" not in contents

    def test_defaults_when_components_missing(self, tmp_path: Path) -> None:
        """username / port / database が欠落していてもデフォルトに置換される。"""
        url = "postgres://:secret@host"
        new_url = _strip_password_to_pgpass(url)
        assert "secret" not in new_url
        pgpass = tmp_path / ".pgpass"
        # user は "*"、port は 5432、db は "*"
        assert "host:5432:*:*:secret" in pgpass.read_text()

    def test_quotes_special_chars_in_username(self, tmp_path: Path) -> None:
        """ユーザ名に URL 予約文字が含まれていても再構築できる。"""
        url = "postgres://user%40org:secret@host:5432/db"
        new_url = _strip_password_to_pgpass(url)
        assert "secret" not in new_url
        # urlparse がデコード済みの username を返すため、再 quote した結果が入る
        assert "user%2540org" in new_url
        # ~/.pgpass にはデコード済みのユーザ名で記録される
        pgpass = tmp_path / ".pgpass"
        assert "host:5432:db:user@org:secret" in pgpass.read_text()

    def test_save_strips_password_to_pgpass(self, tmp_path: Path) -> None:
        """Settings.save() は postgres_url 中のパスワードを ~/.pgpass に移す。"""
        s = Settings()
        s.sync.enabled = True
        s.sync.postgres_url = "postgres://alice:topsecret@db.example/mydb"
        s.save()
        saved = json.loads((tmp_path / "settings.json").read_text())
        assert "topsecret" not in saved["mem"]["sync"]["postgres_url"]
        assert (tmp_path / ".pgpass").exists()

    def test_save_settings_file_chmod(self, tmp_path: Path) -> None:
        """settings.json は 0600 で書き込まれる。"""
        s = Settings()
        s.save()
        mode = (tmp_path / "settings.json").stat().st_mode & 0o777
        assert mode == 0o600

    def test_strip_uses_home_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """HOME 環境変数のないシステムでも安全にフォールバックする。"""
        monkeypatch.delenv("HOME", raising=False)
        # HOME がなくても urlparse の処理自体はパスワードのない URL を返す
        assert _strip_password_to_pgpass("postgres://u@h/db") == "postgres://u@h/db"

    def test_symlink_pgpass_raises(self, tmp_path: Path) -> None:
        """~/.pgpass がシンボリックリンクの場合は OSError（O_NOFOLLOW）。"""
        real_file = tmp_path / "real_pgpass"
        real_file.write_text("", encoding="utf-8")
        pgpass = tmp_path / ".pgpass"
        pgpass.symlink_to(real_file)

        url = "postgres://alice:secret@db.example:5432/mydb"
        with pytest.raises(OSError):
            _strip_password_to_pgpass(url)

    def test_wrong_permissions_fixed(self, tmp_path: Path) -> None:
        """~/.pgpass のパーミッションが 0600 でない場合は 0600 に修正される。"""
        pgpass = tmp_path / ".pgpass"
        pgpass.write_text("", encoding="utf-8")
        pgpass.chmod(0o644)

        url = "postgres://alice:secret@db.example:5432/mydb"
        _strip_password_to_pgpass(url)

        mode = pgpass.stat().st_mode & 0o777
        assert mode == 0o600

    def test_correct_permissions_not_changed(self, tmp_path: Path) -> None:
        """~/.pgpass のパーミッションが既に 0600 の場合はそのまま追記する。"""
        pgpass = tmp_path / ".pgpass"
        pgpass.write_text("", encoding="utf-8")
        pgpass.chmod(0o600)

        url = "postgres://alice:secret@db.example:5432/mydb"
        _strip_password_to_pgpass(url)

        mode = pgpass.stat().st_mode & 0o777
        assert mode == 0o600
        assert "secret" in pgpass.read_text()


class TestLoadAutoStripPassword:
    """Settings.load() によるパスワード自動分離のテスト。"""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))

    def test_load_strips_password_and_rewrites_settings(self, tmp_path: Path) -> None:
        """パスワード付き URL を settings.json に書いた場合、load() 時に自動分離される。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "postgres://alice:secret@db.example:5432/mydb"}}})
        )
        s = Settings.load(settings_path=settings_file)

        # インメモリのURLからパスワードが除去されている
        assert "secret" not in s.sync.postgres_url
        assert s.sync.postgres_url == "postgres://alice@db.example:5432/mydb"

        # settings.json が書き戻されパスワードが除去されている
        raw = json.loads(settings_file.read_text())
        assert "secret" not in raw["mem"]["sync"]["postgres_url"]

        # ~/.pgpass にパスワードが移動している
        pgpass = tmp_path / ".pgpass"
        assert pgpass.exists()
        assert "secret" in pgpass.read_text()

    def test_load_preserves_other_sections_when_stripping(self, tmp_path: Path) -> None:
        """パスワード分離の書き戻し時に他プラグインのセクションが保持される。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({
                "other_plugin": {"key": "value"},
                "mem": {"sync": {"enabled": True, "postgres_url": "postgres://u:pw@h:5432/db"}},
            })
        )
        Settings.load(settings_path=settings_file)

        raw = json.loads(settings_file.read_text())
        assert raw["other_plugin"]["key"] == "value"

    def test_load_no_password_no_rewrite(self, tmp_path: Path) -> None:
        """パスワードのない URL では settings.json を書き戻さない。"""
        settings_file = tmp_path / "settings.json"
        original = json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "postgres://alice@db.example:5432/mydb"}}})
        settings_file.write_text(original)
        mtime_before = settings_file.stat().st_mtime_ns

        Settings.load(settings_path=settings_file)

        # ファイルが更新されていない
        assert settings_file.stat().st_mtime_ns == mtime_before

    def test_load_settings_file_chmod_after_strip(self, tmp_path: Path) -> None:
        """パスワード分離後の settings.json は 0600 になる。"""
        settings_file = tmp_path / "settings.json"
        settings_file.write_text(
            json.dumps({"mem": {"sync": {"enabled": True, "postgres_url": "postgres://u:pw@h:5432/db"}}})
        )
        Settings.load(settings_path=settings_file)

        mode = settings_file.stat().st_mode & 0o777
        assert mode == 0o600
