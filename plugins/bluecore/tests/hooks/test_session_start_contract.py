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
from unittest.mock import MagicMock

from bluecore.hooks import session_install, session_start


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
    import bluecore.mem.settings as settings_mod
    from bluecore.mem import cli

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
        import bluecore.mem.settings as settings_mod

        monkeypatch.setattr(settings_mod.Settings, "load", classmethod(lambda cls: (_ for _ in ()).throw(RuntimeError("settings broken"))))
        monkeypatch.setattr(sys, "argv", ["python", "setup"])
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

        from bluecore.mem import cli

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
        from bluecore.mem import cli

        monkeypatch.setattr(cli, "_initialize_db", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken")))
        stdout, _ = _run_cli_main(["setup"], {}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


class TestMemCliContextContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdout, _ = _run_cli_main(["context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_build_context_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        import bluecore.mem.context as context_mod

        monkeypatch.setattr(context_mod, "build_context", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("ctx broken")))
        stdout, _ = _run_cli_main(["context"], {"cwd": str(tmp_path)}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


class TestMemCliRecordProjectProfileContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdin = {"cwd": str(tmp_path), "languages": ["python"], "primary_language": "python"}
        stdout, _ = _run_cli_main(["record-project-profile"], stdin, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_db_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        from bluecore.mem import cli

        def _failing_open_db(*a, **kw):
            raise RuntimeError("db error")

        monkeypatch.setattr(cli, "_open_db", MagicMock(side_effect=RuntimeError("db error")))
        stdin = {"cwd": str(tmp_path)}
        stdout, _ = _run_cli_main(["record-project-profile"], stdin, monkeypatch, tmp_path)
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
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

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


def test_log_git_info_no_branch() -> None:
    """ブランチ未取得なら git 情報ログを出さない（例外なく完了）。"""
    session_start._log_git_info({"branch": None, "commit_hash": None, "uncommitted_count": 0})


def test_get_git_info_not_in_worktree(monkeypatch) -> None:
    """git work tree 外なら初期値（branch=None）を返す。"""
    monkeypatch.setattr(session_start, "check_output_text", lambda *a, **k: "false")
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    info = session_start._get_git_info()
    assert info["branch"] is None


def test_save_project_profile_error(monkeypatch) -> None:
    """プロファイル保存中の例外はログに記録して握りつぶす。"""
    monkeypatch.setattr(
        session_start, "_build_project_profile",
        lambda pi: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    logs: list[str] = []
    monkeypatch.setattr(session_start, "log", logs.append)
    session_start._save_project_profile(object())
    assert any("save error" in m for m in logs)


def _pi(languages=None, frameworks=None, primary=None):
    from types import SimpleNamespace
    return SimpleNamespace(languages=languages or [], frameworks=frameworks or [], primary_language=primary)


def test_collect_project_context_package_json_and_coverage(monkeypatch, tmp_path) -> None:
    """パッケージマネージャ未検出+package.json有+coverage hint有の経路。"""
    from types import SimpleNamespace

    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source=""))
    monkeypatch.setattr(session_start, "get_selection_prompt", lambda: "select")
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda p: "- cov 100%")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    parts = session_start._collect_project_context(_pi(languages=["typescript"]))
    assert any("coverage_hint" in p for p in parts)


def test_collect_project_context_ruby(monkeypatch, tmp_path) -> None:
    """pm未検出+package.json無+ruby言語の経路。"""
    from types import SimpleNamespace

    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source=""))
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda p: "")
    monkeypatch.chdir(tmp_path)
    session_start._collect_project_context(_pi(languages=["ruby"], frameworks=["rails"]))


def test_collect_project_context_other_language(monkeypatch, tmp_path) -> None:
    """pm未検出+package.json無+ruby以外の言語の経路。"""
    from types import SimpleNamespace

    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=None, source=""))
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda p: "")
    monkeypatch.chdir(tmp_path)
    session_start._collect_project_context(_pi(languages=["go"]))


def test_collect_project_context_frameworks_only(monkeypatch, tmp_path) -> None:
    """言語が空でフレームワークのみでもプロジェクト情報を出力する。"""
    from types import SimpleNamespace

    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name="npm", source="x"))
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda p: "")
    parts = session_start._collect_project_context(_pi(languages=[], frameworks=["rails"]))
    assert any("Project type" in p for p in parts)


def test_dedupe_recent_sessions_keeps_newer(monkeypatch) -> None:
    """同名セッションは新しい mtime の方を残す。"""
    batches = iter([
        [{"path": "/a/x-session.tmp", "mtime": 100}],
        [{"path": "/b/x-session.tmp", "mtime": 50}],
    ])
    monkeypatch.setattr(session_start, "find_files", lambda d, pat, max_age=7: next(batches))
    result = session_start.dedupe_recent_sessions([Path("/a"), Path("/b")])
    assert len(result) == 1
    assert result[0]["mtime"] == 100
