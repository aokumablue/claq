"""settings のテスト。

Settings は SQLite ローカルのみのランタイム設定であり、``last_compacted_at``
（自動圧縮の実行時刻）のみ ``state_path``（``mem_state.json``）に永続化する
構成を検証する。
"""

import json
from pathlib import Path

import pytest

from bluecore.mem.settings import _DEFAULT_EMBEDDING_MODEL, Settings


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
        assert s.embedding_model == _DEFAULT_EMBEDDING_MODEL
        assert s.search_half_life_days == 30.0
        assert s.chunk_max_length == 2000
        assert s.context_chunk_count == 30
        assert s.context_hot_tokens == 400
        assert s.context_hot_hours == 24
        assert s.context_digest_tokens == 800
        assert s.context_digest_count == 12
        assert s.excluded_projects == []

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

    def test_derived_properties(self, tmp_path: Path) -> None:
        s = Settings()
        assert s.data_path == tmp_path
        assert s.db_path == tmp_path / "mem.db"
        assert s.log_dir == tmp_path / "logs"
        assert s.state_path == tmp_path / "mem_state.json"


class TestSettingsSave:
    """永続化のテスト"""

    def test_save_creates_file(self, tmp_path: Path) -> None:
        s = Settings()
        s.save()
        assert (tmp_path / "mem_state.json").exists()

    def test_save_only_writes_last_compacted_at(self, tmp_path: Path) -> None:
        """save() は last_compacted_at のみを書き出す。"""
        s = Settings(last_compacted_at=123.0)
        s.save()
        raw = json.loads((tmp_path / "mem_state.json").read_text())
        assert raw == {"last_compacted_at": 123.0}

    def test_save_state_file_chmod(self, tmp_path: Path) -> None:
        """state ファイルは 0600 で書き込まれる。"""
        s = Settings()
        s.save()
        mode = (tmp_path / "mem_state.json").stat().st_mode & 0o777
        assert mode == 0o600


class TestSettingsLoad:
    """読み込みのテスト"""

    def test_load_nonexistent(self, tmp_path: Path) -> None:
        path = tmp_path / "nonexistent.json"
        s = Settings.load(state_path=path)
        assert s.log_level == "info"
        assert s.last_compacted_at == 0.0

    def test_load_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "mem_state.json"
        path.write_text("not json{{{")
        s = Settings.load(state_path=path)
        assert s.log_level == "info"
        assert s.last_compacted_at == 0.0

    def test_load_invalid_json_with_default_path(self, tmp_path: Path) -> None:
        (tmp_path / "mem_state.json").write_text("not json{{{")
        s = Settings.load()
        assert s.log_level == "info"
        assert s.last_compacted_at == 0.0

    def test_load_unreadable_file_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """state ファイルの read_text が OSError を起こしても既定値で返す。"""
        state_file = tmp_path / "mem_state.json"
        state_file.write_text("{}")

        call_count = {"n": 0}
        original_read_text = Path.read_text

        def _patched_read_text(self_path: Path, *args, **kwargs):  # type: ignore[override]
            if str(self_path) == str(state_file) and call_count["n"] == 0:
                call_count["n"] += 1
                raise OSError("permission denied")
            return original_read_text(self_path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _patched_read_text)

        s = Settings.load(state_path=state_file)
        assert s.last_compacted_at == 0.0

    def test_load_non_dict_state_is_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "mem_state.json"
        path.write_text("[]")
        s = Settings.load(state_path=path)
        assert s.last_compacted_at == 0.0

    def test_load_unknown_keys_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "mem_state.json"
        path.write_text(json.dumps({"unknown_key": "val"}))
        s = Settings.load(state_path=path)
        assert not hasattr(s, "unknown_key")

    def test_load_restores_last_compacted_at(self, tmp_path: Path) -> None:
        path = tmp_path / "mem_state.json"
        path.write_text(json.dumps({"last_compacted_at": 999.0}))
        s = Settings.load(state_path=path)
        assert s.last_compacted_at == 999.0

    def test_load_default_reads_state_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """デフォルトパスで mem_state.json があれば自動で読み込む。"""
        import bluecore.mem.settings as mod

        monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)
        (tmp_path / "mem_state.json").write_text(json.dumps({"last_compacted_at": 77.0}))

        s = Settings.load()
        assert s.last_compacted_at == 77.0

    def test_load_default_nonexistent_does_not_create_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """デフォルトパスに state ファイルが無くても load() は作成しない。"""
        import bluecore.mem.settings as mod

        monkeypatch.setattr(mod, "_DEFAULT_DATA_DIR", tmp_path)
        Settings.load()
        assert not (tmp_path / "mem_state.json").exists()


class TestAutoCompactSettings:
    """自動圧縮設定のテスト（ハードコード値 + 永続状態）"""

    def test_auto_compact_defaults(self) -> None:
        s = Settings()
        assert s.auto_compact_enabled is True
        assert s.auto_compact_interval_days == 7
        assert s.last_compacted_at == 0.0

    def test_last_compacted_at_roundtrip(self, tmp_path: Path) -> None:
        """last_compacted_at は save() -> load() で復元される。"""
        state_file = tmp_path / "mem_state.json"
        s = Settings(last_compacted_at=1234567890.0)
        s.save()

        loaded = Settings.load(state_path=state_file)
        assert loaded.last_compacted_at == 1234567890.0
        # auto_compact_enabled / interval_days はハードコード既定値
        assert loaded.auto_compact_enabled is True
        assert loaded.auto_compact_interval_days == 7
