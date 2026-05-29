"""SessionStart hook の JSON 出力契約テスト。

各 SessionStart hook が失敗・例外・import 失敗時でも
必ず有効な hookSpecificOutput JSON を stdout に返すことを保証する。
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

from devgear.hooks import hook_common, session_install, session_start
from devgear.hooks.hook_common import emit_session_start_output


def _assert_session_start_json(output: str) -> dict:
    """stdout が有効な SessionStart JSON かを検証する。"""
    stripped = output.strip()
    assert stripped, f"stdout が空: {output!r}"
    payload = json.loads(stripped)
    assert "hookSpecificOutput" in payload, f"hookSpecificOutput なし: {payload}"
    inner = payload["hookSpecificOutput"]
    assert inner.get("hookEventName") == "SessionStart", f"hookEventName 不正: {inner}"
    assert "additionalContext" in inner, f"additionalContext なし: {inner}"
    return inner


def _run_cli_main(argv: list[str], stdin_json: dict, monkeypatch, tmp_path: Path) -> tuple[str, str]:
    """mem.cli.main() を実行して stdout/stderr を返す。"""
    import devgear.mem.settings as settings_mod
    from devgear.mem import cli

    monkeypatch.setattr(settings_mod, "_DEFAULT_DATA_DIR", tmp_path)
    monkeypatch.setattr(sys, "argv", ["python", *argv])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_json)))

    buf_out = io.StringIO()
    buf_err = io.StringIO()
    with redirect_stdout(buf_out), redirect_stderr(buf_err):
        try:
            cli.main()
        except SystemExit as exc:
            if exc.code not in (0, None):
                raise
    return buf_out.getvalue(), buf_err.getvalue()


class TestMemCliSetupContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdout, _ = _run_cli_main(["setup"], {}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_settings_load_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        import devgear.mem.settings as settings_mod

        monkeypatch.setattr(settings_mod.Settings, "load", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("settings broken"))))
        monkeypatch.setattr(sys, "argv", ["python", "setup"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

        from devgear.mem import cli

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            try:
                cli.main()
            except SystemExit:
                pass
        _assert_session_start_json(buf_out.getvalue())

    def test_db_init_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        """DB 初期化失敗でも JSON を返す。"""
        from devgear.mem import cli

        monkeypatch.setattr(cli, "_initialize_db", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken")))
        stdout, _ = _run_cli_main(["setup"], {}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


class TestMemCliContextContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdout, _ = _run_cli_main(["context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_build_context_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        import devgear.mem.context as context_mod

        monkeypatch.setattr(context_mod, "build_context", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ctx broken")))
        stdout, _ = _run_cli_main(["context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


class TestMemCliRecordProjectProfileContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdin = {"cwd": str(tmp_path), "languages": ["python"], "primary_language": "python"}
        stdout, _ = _run_cli_main(["record-project-profile"], stdin, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_db_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        from devgear.mem import cli

        def _failing_open_db(*a, **kw):
            raise RuntimeError("db error")

        monkeypatch.setattr(cli, "_open_db", MagicMock(side_effect=RuntimeError("db error")))
        stdin = {"cwd": str(tmp_path)}
        stdout, _ = _run_cli_main(["record-project-profile"], stdin, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


class TestMemCliTeamContextContract:
    def test_pg_disabled_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdout, _ = _run_cli_main(["team-context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_pg_connection_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        import devgear.mem.settings as settings_mod
        from devgear.mem.settings import Settings

        pg_settings = MagicMock(spec=Settings)
        pg_settings.team = MagicMock(enabled=True, exclude_self=False)
        pg_settings.sync = MagicMock(enabled=True, postgres_url="postgresql://localhost/test")
        pg_settings.excluded_projects = []
        monkeypatch.setattr(settings_mod.Settings, "load", classmethod(lambda cls: pg_settings))

        with patch("devgear.mem.pg_database.PgDatabase") as mock_pg_cls:
            mock_pg = MagicMock()
            mock_pg.test_connection.return_value = False
            mock_pg_cls.return_value = mock_pg
            stdout, _ = _run_cli_main(["team-context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)

        _assert_session_start_json(stdout)


class TestSessionInstallContract:
    def test_install_sh_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"version": "0.0.99"}))
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.1\n")

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "install failed"

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)

        result = session_install.run("")
        _assert_session_start_json(result)
        assert version_file.read_text() == "0.0.1\n"

    def test_main_exception_emits_session_start(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(session_install, "run", lambda _: (_ for _ in ()).throw(RuntimeError("crash")))

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = session_install.main()
        assert code == 0
        _assert_session_start_json(buf_out.getvalue())


class TestSessionStartHookContract:
    def test_run_exception_emits_session_start_from_main(self, monkeypatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(session_start, "run", lambda _: (_ for _ in ()).throw(RuntimeError("hook crash")))

        buf_out = io.StringIO()
        buf_err = io.StringIO()
        with redirect_stdout(buf_out), redirect_stderr(buf_err):
            code = session_start.main()
        assert code == 0
        _assert_session_start_json(buf_out.getvalue())

    def test_get_git_info_outside_repo_no_error_logs(self, monkeypatch) -> None:
        """git 管理外ディレクトリから呼ばれた場合、個別失敗ログを出さず空の dict を返す。"""
        import subprocess

        call_count = 0

        def _fake_check_output_text(cmd: list, **kwargs) -> str:
            nonlocal call_count
            call_count += 1
            if "--is-inside-work-tree" in cmd:
                raise subprocess.CalledProcessError(128, cmd)
            raise AssertionError("後続の git コマンドは呼ばれるべきではない")

        # session_start モジュール内でバインドされた名前をパッチする
        monkeypatch.setattr(session_start, "check_output_text", _fake_check_output_text)

        messages: list[str] = []
        monkeypatch.setattr(session_start, "log", lambda msg, *a, **kw: messages.append(str(msg)))

        result = session_start._get_git_info()

        assert result == {"branch": None, "commit_hash": None, "uncommitted_count": 0}
        # --is-inside-work-tree の 1 回だけ呼ばれ、branch/commit/status は呼ばれない
        assert call_count == 1
        # 個別失敗ログ（"failed:" を含む）が出ていないことを確認
        assert not any("failed:" in m for m in messages)


class TestRunWithFlagsSessionStartContract:
    def test_fallback_output_is_valid_json(self) -> None:
        result = emit_session_start_output()
        _assert_session_start_json(result)

    def test_session_start_hook_ids_in_hook_common(self) -> None:
        """SESSION_START_HOOK_IDS が hook_common の single source of truth から取得されること。"""
        from devgear.hooks.run_with_flags import SESSION_START_HOOK_IDS as rwf_ids

        assert rwf_ids is hook_common.SESSION_START_HOOK_IDS

    def test_child_nonzero_return_suppressed_for_session_start(self, monkeypatch, tmp_path: Path) -> None:
        """SessionStart 系の hook で子プロセスが非 0 を返しても returncode を 0 にする。"""
        import subprocess

        import devgear.lib.hook_flags as flags_mod
        from devgear.hooks import run_with_flags

        monkeypatch.setattr(flags_mod, "is_hook_enabled", lambda *a, **kw: True)

        mock_result = MagicMock()
        mock_result.stdout = hook_common.emit_session_start_output()
        mock_result.stderr = ""
        mock_result.returncode = 1

        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: mock_result)
        monkeypatch.setattr(sys, "argv", ["rwf", "session:start", "devgear.hooks.session_start", "minimal"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

        ret = run_with_flags.main()
        assert ret == 0
