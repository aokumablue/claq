"""bluecore.mem.cli のテスト"""

from __future__ import annotations

import io
import json
import runpy
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from bluecore.mem import cli
from bluecore.mem.database import Database
from bluecore.mem.models import Repo


def _run_cli(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    argv: list[str],
    stdin_payload: dict,
) -> tuple[str, str, int]:
    """データディレクトリを tmp_path に差し替えて cli.main() を実行する。"""
    import bluecore.mem.settings as settings_mod

    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_payload)))

    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        exit_code = cli.main()
    return stdout.getvalue(), stderr.getvalue(), exit_code


class TestSetup:
    """setup コマンド（SessionStart フック経路）。"""

    def test_creates_database_and_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DB を作成し、SessionStart 契約の JSON を stdout に出す。"""
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"], {})
        assert exit_code == 0
        assert stderr == ""
        payload = json.loads(stdout)
        assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert (tmp_path / "mem.db").exists()

    def test_swallows_db_failure(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """DB 初期化が失敗してもフックを壊さず JSON を返す。"""
        monkeypatch.setattr(
            cli, "_initialize_db", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"], {})
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_settings_failure_still_emits_session_start_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """設定ロード失敗でも exit_code=0 と JSON 出力を維持する。"""
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"], {})
        assert exit_code == 0
        assert stderr == ""
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"

    def test_handler_exception_keeps_exit_code_1_but_emits_json(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ハンドラ例外時も JSON を出しつつ exit_code=1 を返す。"""
        monkeypatch.setattr(
            cli, "_run_session_start_command", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["setup"], {})
        assert exit_code == 1
        assert "boom" in stderr
        assert json.loads(stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"


class TestInit:
    """init コマンド（DB 再作成）。"""

    def test_recreates_database_dropping_old_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既存 DB と sidecar を破棄して空の DB を作り直す。"""
        db_path = tmp_path / "mem.db"
        with Database(db_path) as db:
            db.upsert_repo(Repo(id="old", identity_key="key-old", root_path="/tmp/old"))
        for suffix in ("-wal", "-shm", "-journal"):
            (tmp_path / f"mem.db{suffix}").write_text("stale", encoding="utf-8")

        stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"], {})
        assert exit_code == 0
        assert (stdout, stderr) == ("", "")
        for suffix in ("-wal", "-shm", "-journal"):
            assert not (tmp_path / f"mem.db{suffix}").exists()

        with Database(db_path) as db:
            assert db.list_repos() == []

    def test_remove_db_artifacts_tolerates_missing_files(self, tmp_path: Path) -> None:
        """存在しないファイルがあっても例外を出さない。"""
        db_path = tmp_path / "mem.db"
        db_path.write_text("db", encoding="utf-8")
        (tmp_path / "mem.db-journal").write_text("stale", encoding="utf-8")

        cli._remove_db_artifacts(db_path)

        assert not db_path.exists()
        assert not (tmp_path / "mem.db-journal").exists()


class TestArgvAndStdin:
    """引数と stdin の解釈。"""

    def test_no_command_prints_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """引数なしは HELP_TEXT を出力する。"""
        monkeypatch.setattr(sys, "argv", ["python"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert cli.main() == 0
        assert "CLI Commands for mem" in stdout.getvalue()

    def test_help_flag_prints_help(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--help も HELP_TEXT を出力する。"""
        monkeypatch.setattr(sys, "argv", ["python", "--help"])
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            assert cli.main() == 0
        assert "CLI Commands for mem" in stdout.getvalue()

    def test_unknown_command_returns_2(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """未知のコマンドは exit_code=2。"""
        _stdout, _stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["nonexistent"], {})
        assert exit_code == 2

    def test_tty_stdin_is_not_read(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin が tty なら読み取らず空 dict を返す。"""
        monkeypatch.setattr(sys, "argv", ["python", "init"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        assert cli._parse_argv_and_stdin() == ("init", {})

    @pytest.mark.parametrize("raw", ["", "   ", "[1, 2]", "{ broken"])
    def test_unusable_stdin_yields_empty_dict(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        """空・非 dict・不正 JSON はすべて空 dict に落とす。"""
        monkeypatch.setattr(sys, "argv", ["python", "init"])
        monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
        assert cli._parse_argv_and_stdin() == ("init", {})

    def test_valid_stdin_is_parsed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """dict の JSON はそのまま返る。"""
        monkeypatch.setattr(sys, "argv", ["python", "init"])
        monkeypatch.setattr(sys, "stdin", io.StringIO('{"cwd": "/tmp"}'))
        assert cli._parse_argv_and_stdin() == ("init", {"cwd": "/tmp"})

    def test_stdin_os_error_is_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stdin の読み取りが OSError でも空 dict に落とす。"""
        class _BrokenStdin:
            def isatty(self) -> bool:
                return False

            def read(self) -> str:
                raise OSError("stdin gone")

        monkeypatch.setattr(sys, "argv", ["python", "init"])
        monkeypatch.setattr(sys, "stdin", _BrokenStdin())
        assert cli._parse_argv_and_stdin() == ("init", {})


class TestNormalCommandFailures:
    """SessionStart 以外のコマンドのエラー経路。"""

    def test_settings_failure_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """設定ロード失敗は exit_code=1 と stderr 出力。"""
        monkeypatch.setattr(
            cli, "_load_settings_or_raise", lambda: (_ for _ in ()).throw(RuntimeError("設定失敗"))
        )
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"], {})
        assert exit_code == 1
        assert "設定/ログ初期化失敗" in stderr

    def test_handler_exception_returns_1(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """ハンドラ例外は exit_code=1 と stderr 出力。"""
        monkeypatch.setattr(
            cli, "_run_normal_command", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("init failure"))
        )
        _stdout, stderr, exit_code = _run_cli(monkeypatch, tmp_path, ["init"], {})
        assert exit_code == 1
        assert "init failure" in stderr


def test_mem_main_module_invokes_cli_main(monkeypatch: pytest.MonkeyPatch) -> None:
    """python -m bluecore.mem が cli.main() を呼ぶ。"""
    monkeypatch.setattr(sys, "argv", ["python", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("bluecore.mem.__main__", run_name="__main__")

    assert excinfo.value.code == 0
