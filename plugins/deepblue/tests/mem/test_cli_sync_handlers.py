"""cli_sync_handlers のテスト。

handle_sync_check の可視化改善と handle_sync_status の JSON 出力契約を検証する。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def mock_settings(tmp_path):
    """sync 設定を持つモック Settings。"""
    settings = MagicMock()
    settings.db_path = tmp_path / "test.db"
    settings.sync.enabled = True
    settings.sync.postgres_url = "postgresql://user:TESTPASSWORD@host:5432/db"
    settings.sync.interval_hours = 3
    settings.sync.last_synced_at = 0.0
    settings.sync.last_sync_attempt_at = 0.0
    settings.sync.last_sync_success = False
    return settings


class TestHandleSyncCheckVisibility:
    """handle_sync_check の可視化改善テスト。"""

    def test_logs_info_on_skip(self, mock_settings, monkeypatch):
        """should_sync=False 時に info ログが出ることを確認する。"""
        # 遅延 import のため devgear.mem.sync 側でパッチする
        monkeypatch.setattr("devgear.mem.sync.should_sync", lambda _: False)
        log = MagicMock()

        from devgear.mem.cli_sync_handlers import handle_sync_check

        handle_sync_check(mock_settings, log=log)

        log.info.assert_called()

    def test_logs_error_on_failure(self, mock_settings, monkeypatch, capsys):
        """同期失敗時に error ログと stderr 出力が出ることを確認する。"""
        from devgear.mem.sync import SyncResult

        monkeypatch.setattr("devgear.mem.sync.should_sync", lambda _: True)
        monkeypatch.setattr(
            "devgear.mem.sync.sync_to_postgres",
            lambda _: SyncResult(success=False, error="接続エラー"),
        )
        log = MagicMock()

        from devgear.mem.cli_sync_handlers import handle_sync_check

        handle_sync_check(mock_settings, log=log)

        log.error.assert_called()
        captured = capsys.readouterr()
        assert "[sync-check]" in captured.err
        assert "接続エラー" in captured.err

    def test_no_error_when_success(self, mock_settings, monkeypatch, capsys):
        """同期成功時に stderr 出力がないことを確認する。"""
        from devgear.mem.sync import SyncResult

        monkeypatch.setattr("devgear.mem.sync.should_sync", lambda _: True)
        monkeypatch.setattr(
            "devgear.mem.sync.sync_to_postgres",
            lambda _: SyncResult(success=True, chunks=5),
        )
        log = MagicMock()

        from devgear.mem.cli_sync_handlers import handle_sync_check

        handle_sync_check(mock_settings, log=log)

        captured = capsys.readouterr()
        assert captured.err == ""


class TestHandleSyncStatus:
    """handle_sync_status の JSON 出力契約テスト。"""

    @pytest.mark.parametrize(
        "scenario, pg_url, psycopg_ok, conn_ok, expect_connection",
        [
            ("env設定済/接続OK", "postgresql://user:pass@host:5432/db", True, True, "ok"),
            ("url未設定", "", True, False, "skipped"),
            ("psycopg未導入", "postgresql://user@host/db", False, False, "skipped"),
            ("接続失敗", "postgresql://user@host/db", True, False, "failed"),
        ],
    )
    def test_sync_status_json_structure(
        self,
        mock_settings,
        monkeypatch,
        capsys,
        scenario,
        pg_url,
        psycopg_ok,
        conn_ok,
        expect_connection,
    ):
        """sync-status の各シナリオで JSON 構造が正しいことを確認する。"""
        mock_settings.sync.postgres_url = pg_url
        mock_settings.sync.enabled = bool(pg_url)

        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._check_psycopg", lambda: psycopg_ok
        )
        if pg_url and psycopg_ok:
            conn_result = ("ok", None) if conn_ok else ("failed", "接続テスト失敗")
        else:
            conn_result = ("skipped", None) if not pg_url else ("skipped", "psycopg が未インストールです")
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._test_pg_connection",
            lambda url, installed: conn_result,
        )
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._count_all_pending", lambda _: 7
        )

        from devgear.mem.cli_sync_handlers import handle_sync_status

        handle_sync_status(mock_settings, {})

        captured = capsys.readouterr()
        output = json.loads(captured.out)

        # 必須フィールドが全て存在すること
        required_fields = [
            "enabled", "postgres_url_set", "postgres_url_masked",
            "psycopg_installed", "connection", "connection_error",
            "pending_rows", "last_sync_success", "last_sync_at",
        ]
        for field in required_fields:
            assert field in output, f"{scenario}: {field} が出力に含まれていない"

        assert output["connection"] == expect_connection, f"{scenario}: connection が期待値と異なる"

    def test_password_masked_in_url(self, mock_settings, monkeypatch, capsys):
        """postgres_url_masked にパスワードが含まれないことを確認する。"""
        mock_settings.sync.postgres_url = "postgresql://user:TESTPASSWORD123@host:5432/db"
        mock_settings.sync.enabled = True

        monkeypatch.setattr("devgear.mem.cli_sync_handlers._check_psycopg", lambda: False)
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._test_pg_connection",
            lambda url, installed: ("skipped", "psycopg が未インストールです"),
        )
        monkeypatch.setattr("devgear.mem.cli_sync_handlers._count_all_pending", lambda _: 0)

        from devgear.mem.cli_sync_handlers import handle_sync_status

        handle_sync_status(mock_settings, {})

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "TESTPASSWORD123" not in (output["postgres_url_masked"] or "")
        assert "***" in (output["postgres_url_masked"] or "")

    def test_url_none_when_not_set(self, mock_settings, monkeypatch, capsys):
        """postgres_url 未設定時に postgres_url_masked が null になることを確認する。"""
        mock_settings.sync.postgres_url = ""
        mock_settings.sync.enabled = False

        monkeypatch.setattr("devgear.mem.cli_sync_handlers._check_psycopg", lambda: True)
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._test_pg_connection",
            lambda url, installed: ("skipped", None),
        )
        monkeypatch.setattr("devgear.mem.cli_sync_handlers._count_all_pending", lambda _: 0)

        from devgear.mem.cli_sync_handlers import handle_sync_status

        handle_sync_status(mock_settings, {})

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["postgres_url_masked"] is None
        assert output["connection"] == "skipped"


class TestBuildSyncStatusDict:
    """_build_sync_status_dict のテスト。"""

    def test_lite_mode_skips_connection(self, mock_settings, monkeypatch):
        """lite=True 時に _test_pg_connection が呼ばれず connection=skipped になる。"""
        called = []
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._check_psycopg", lambda: True
        )
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._test_pg_connection",
            lambda url, installed: called.append(True) or ("ok", None),
        )

        from devgear.mem.cli_sync_handlers import _build_sync_status_dict

        result = _build_sync_status_dict(mock_settings, lite=True)

        assert result["connection"] == "skipped"
        assert result["pending_rows"] is None
        assert called == [], "_test_pg_connection が呼ばれてはいけない"

    def test_full_mode_calls_connection(self, mock_settings, monkeypatch):
        """lite=False（デフォルト）時に接続テストが実行される。"""
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._check_psycopg", lambda: True
        )
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._test_pg_connection",
            lambda url, installed: ("ok", None),
        )
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._count_all_pending", lambda _: 3
        )

        from devgear.mem.cli_sync_handlers import _build_sync_status_dict

        result = _build_sync_status_dict(mock_settings, lite=False)

        assert result["connection"] == "ok"
        assert result["pending_rows"] == 3


class TestBuildSyncRecommendations:
    """cli.py の _build_sync_recommendations の全分岐テスト。"""

    @pytest.mark.parametrize(
        "scenario, status_override, expected_keyword",
        [
            (
                "url未設定",
                {"postgres_url_set": False, "psycopg_installed": True, "connection": "skipped", "connection_error": None},
                "postgres_url",
            ),
            (
                "psycopg未インストール",
                {"postgres_url_set": True, "psycopg_installed": False, "connection": "skipped", "connection_error": None},
                "psycopg",
            ),
            (
                "接続失敗",
                {"postgres_url_set": True, "psycopg_installed": True, "connection": "failed", "connection_error": "タイムアウト"},
                "タイムアウト",
            ),
        ],
    )
    def test_each_branch(self, scenario, status_override, expected_keyword):
        """各条件で対応する推奨メッセージが1件以上生成されることを確認する。"""
        from devgear.mem.cli import _build_sync_recommendations
        from devgear.mem.cli_sync_handlers import SyncStatusDict

        base: SyncStatusDict = {
            "enabled": True,
            "postgres_url_set": True,
            "postgres_url_masked": "postgresql://user:***@host/db",
            "psycopg_installed": True,
            "connection": "ok",
            "connection_error": None,
            "pending_rows": 0,
            "last_sync_success": True,
            "last_sync_at": 0.0,
        }
        base.update(status_override)  # type: ignore[typeddict-item]
        recs = _build_sync_recommendations(base)
        assert any(expected_keyword in r for r in recs), f"{scenario}: '{expected_keyword}' を含む推奨が出ない"

    def test_no_recs_when_all_ok(self):
        """全て正常な場合に推奨が空になることを確認する。"""
        from devgear.mem.cli import _build_sync_recommendations
        from devgear.mem.cli_sync_handlers import SyncStatusDict

        status: SyncStatusDict = {
            "enabled": True,
            "postgres_url_set": True,
            "postgres_url_masked": "postgresql://user:***@host/db",
            "psycopg_installed": True,
            "connection": "ok",
            "connection_error": None,
            "pending_rows": 0,
            "last_sync_success": True,
            "last_sync_at": 0.0,
        }
        assert _build_sync_recommendations(status) == []


class TestHandleSetup:
    """_handle_setup の sync 診断テスト。"""

    def test_logs_warning_when_initialize_db_fails(self, mock_settings, monkeypatch, caplog):
        """_initialize_db が例外を投げた場合に warning ログが出ることを確認する。"""
        import logging

        monkeypatch.setattr(
            "devgear.mem.cli._initialize_db",
            lambda settings, **kw: (_ for _ in ()).throw(RuntimeError("db error")),
        )
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._build_sync_status_dict",
            lambda settings, lite: {
                "enabled": True, "postgres_url_set": True, "postgres_url_masked": None,
                "psycopg_installed": True, "connection": "skipped", "connection_error": None,
                "pending_rows": None, "last_sync_success": True, "last_sync_at": None,
            },
        )

        from devgear.mem.cli import _handle_setup

        with caplog.at_level(logging.WARNING, logger="devgear.mem.CLI"):
            _handle_setup(mock_settings)

        assert any("setup 失敗" in r.message and r.levelno == logging.WARNING for r in caplog.records)

    def test_emits_sync_diagnostics(self, mock_settings, monkeypatch, caplog):
        """_handle_setup が sync 設定を info ログに出すことを確認する。"""
        import logging

        from devgear.mem.cli_sync_handlers import SyncStatusDict

        dummy_status: SyncStatusDict = {
            "enabled": True,
            "postgres_url_set": False,
            "postgres_url_masked": None,
            "psycopg_installed": True,
            "connection": "skipped",
            "connection_error": None,
            "pending_rows": None,
            "last_sync_success": False,
            "last_sync_at": None,
        }
        monkeypatch.setattr(
            "devgear.mem.cli_sync_handlers._build_sync_status_dict",
            lambda settings, lite: dummy_status,
        )
        # DB 初期化をスキップして診断ログ部分だけテストする
        monkeypatch.setattr("devgear.mem.cli._initialize_db", lambda settings, **kw: None)

        from devgear.mem.cli import _handle_setup

        with caplog.at_level(logging.INFO, logger="devgear.mem.CLI"):
            _handle_setup(mock_settings)

        assert any("sync 設定" in r.message for r in caplog.records)
        # url未設定のため推奨 warning が出る
        assert any("setup 推奨" in r.message and r.levelno == logging.WARNING for r in caplog.records)


class TestInternalHelpers:
    """内部ヘルパー関数のテスト。"""

    def test_check_psycopg_true_when_available(self, monkeypatch):
        """psycopg がインポートできる場合 True を返す。"""
        import sys
        import types

        fake_psycopg = types.ModuleType("psycopg")
        monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)

        from devgear.mem.cli_sync_handlers import _check_psycopg

        assert _check_psycopg() is True

    def test_check_psycopg_false_when_missing(self, monkeypatch):
        """psycopg がない場合 False を返す。"""
        import sys
        sys.modules.pop("psycopg", None)

        import builtins
        original_import = builtins.__import__

        def mock_import(name, *args, **kwargs):  # noqa: ANN001
            if name == "psycopg":
                raise ImportError("mocked")
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", mock_import)

        from devgear.mem.cli_sync_handlers import _check_psycopg
        assert _check_psycopg() is False

    def test_test_pg_connection_skipped_when_no_url(self):
        """postgres_url が空の場合 skipped を返す。"""
        from devgear.mem.cli_sync_handlers import _test_pg_connection

        status, error = _test_pg_connection("", True)
        assert status == "skipped"
        assert error is None

    def test_test_pg_connection_skipped_when_psycopg_missing(self):
        """psycopg 未インストールの場合 skipped を返す。"""
        from devgear.mem.cli_sync_handlers import _test_pg_connection

        status, error = _test_pg_connection("postgresql://host/db", False)
        assert status == "skipped"
        assert error is not None

    def test_test_pg_connection_failed_when_test_returns_false(self, monkeypatch):
        """test_connection() が False を返す場合に failed を返す。"""
        class FalsePgDb:
            def __init__(self, url, **kwargs):  # noqa: ANN001
                pass

            def test_connection(self) -> bool:
                return False

            def close(self) -> None:
                pass

        monkeypatch.setattr("devgear.mem.pg_database.PgDatabase", FalsePgDb)

        from devgear.mem.cli_sync_handlers import _test_pg_connection

        status, error = _test_pg_connection("postgresql://host/db", True)
        assert status == "failed"
        assert error == "接続テスト失敗"

    def test_test_pg_connection_failed_on_exception(self, monkeypatch):
        """PgDatabase 接続例外時に failed と固定メッセージを返す。"""
        class BoomPgDb:
            def __init__(self, url, **kwargs):  # noqa: ANN001
                pass

            def test_connection(self):
                raise RuntimeError("boom")

            def close(self):
                pass

        monkeypatch.setattr("devgear.mem.pg_database.PgDatabase", BoomPgDb)

        from devgear.mem.cli_sync_handlers import _test_pg_connection

        status, error = _test_pg_connection("postgresql://host/db", True)
        assert status == "failed"
        # 例外の元メッセージではなく固定文字列を返す
        assert "接続に失敗しました" in (error or "")
        assert "boom" not in (error or "")

    def test_count_all_pending_returns_zero_on_exception(self, mock_settings, monkeypatch):
        """DB 接続失敗時に 0 を返して例外を伝播させないことを確認する。"""
        class BoomDatabase:
            def __init__(self, path):  # noqa: ANN001
                raise RuntimeError("no db")

        monkeypatch.setattr("devgear.mem.database.Database", BoomDatabase)

        from devgear.mem.cli_sync_handlers import _count_all_pending

        assert _count_all_pending(mock_settings) == 0

    def test_count_all_pending_returns_total_on_success(self, mock_settings, monkeypatch):
        """正常系で全テーブルの合計行数を返すことを確認する。"""
        import sqlite3

        conn = sqlite3.connect(":memory:")
        monkeypatch.setattr(
            "devgear.mem.sync._count_pending_rows",
            lambda conn, table: 3,
        )

        class FakeDatabase:
            def __init__(self, path):  # noqa: ANN001
                self.conn = conn

            def close(self) -> None:
                conn.close()

        monkeypatch.setattr("devgear.mem.database.Database", FakeDatabase)

        from devgear.mem.cli_sync_handlers import _count_all_pending
        from devgear.mem.sync import _SYNC_TABLES

        result = _count_all_pending(mock_settings)
        assert result == 3 * len(_SYNC_TABLES)


class TestSplitPassword:
    """_split_password のテーブル駆動テスト。"""

    def test_url_with_password_splits(self) -> None:
        from devgear.mem.cli_sync_handlers import _split_password

        url, pw = _split_password("postgresql://user:secret@host/db")
        assert pw == "secret"
        assert "secret" not in url
        assert "user" in url
        assert "host" in url

    def test_url_without_password_unchanged(self) -> None:
        from devgear.mem.cli_sync_handlers import _split_password

        original = "postgresql://user@host/db"
        url, pw = _split_password(original)
        assert pw is None
        assert url == original

    def test_url_preserves_port(self) -> None:
        from devgear.mem.cli_sync_handlers import _split_password

        url, pw = _split_password("postgresql://user:pass@host:5433/mydb")
        assert pw == "pass"
        assert "5433" in url
        assert "pass" not in url

    def test_empty_url_returns_unchanged(self) -> None:
        from devgear.mem.cli_sync_handlers import _split_password

        url, pw = _split_password("")
        assert pw is None
        assert url == ""


class TestWritePgpass:
    """_write_pgpass のテスト。"""

    def test_creates_pgpass_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from devgear.mem.cli_sync_handlers import _write_pgpass

        monkeypatch.setenv("HOME", str(tmp_path))
        _write_pgpass("myhost", 5432, "mydb", "myuser", "mypassword")
        pgpass_path = tmp_path / ".pgpass"
        assert pgpass_path.exists()
        content = pgpass_path.read_text()
        assert "myhost:5432:mydb:myuser:mypassword" in content

    def test_pgpass_chmod_0600(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from devgear.mem.cli_sync_handlers import _write_pgpass

        monkeypatch.setenv("HOME", str(tmp_path))
        _write_pgpass("host", 5432, "db", "user", "pass")
        pgpass_path = tmp_path / ".pgpass"
        assert pgpass_path.stat().st_mode & 0o777 == 0o600

    def test_no_duplicate_entry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from devgear.mem.cli_sync_handlers import _write_pgpass

        monkeypatch.setenv("HOME", str(tmp_path))
        _write_pgpass("h", 5432, "d", "u", "p")
        _write_pgpass("h", 5432, "d", "u", "p")
        content = (tmp_path / ".pgpass").read_text()
        assert content.count("h:5432:d:u:p") == 1
