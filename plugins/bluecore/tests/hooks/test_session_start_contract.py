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

from bluecore.hooks import session_start


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


class TestMemCliContextContract:
    def test_normal_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        stdout, _ = _run_cli_main(["context"], {}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)

    def test_settings_init_failure_emits_session_start(self, monkeypatch, tmp_path: Path) -> None:
        from bluecore.mem import cli as cli_mod

        monkeypatch.setattr(cli_mod, "Settings", lambda: (_ for _ in ()).throw(RuntimeError("settings broken")))
        monkeypatch.setattr(sys, "argv", ["python", "context"])
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

        monkeypatch.setattr(cli, "_build_context", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("db broken")))
        stdout, _ = _run_cli_main(["context"], {}, monkeypatch, tmp_path)
        _assert_session_start_json(stdout)


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

    def test_grok_symlink_success_logs(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Grok symlink 成功時はログし、SessionStart JSON を返す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BLUECORE_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "bluecore.lib.grok_plugin_root.ensure_grok_plugin_root_symlink",
            lambda: tmp_path / "installed",
        )
        result = session_start.run("")
        _assert_session_start_json(result)
        assert "Grok plugin root symlink" in capsys.readouterr().err

    def test_grok_symlink_exception_is_fail_open(self, monkeypatch, tmp_path: Path, capsys) -> None:
        """Grok symlink 例外は fail-open で SessionStart JSON を返す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BLUECORE_HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(
            "bluecore.lib.grok_plugin_root.ensure_grok_plugin_root_symlink",
            lambda: (_ for _ in ()).throw(OSError("x")),
        )
        result = session_start.run("")
        _assert_session_start_json(result)
        assert "Grok symlink 修復スキップ" in capsys.readouterr().err

def _pi(languages=None, frameworks=None, primary=None):
    from types import SimpleNamespace
    return SimpleNamespace(languages=languages or [], frameworks=frameworks or [], primary_language=primary)


def _stub_project_context(monkeypatch, tmp_path, *, pm_name=None, pm_source="", coverage=""):
    """_collect_project_context の外部依存を固定する。"""
    from types import SimpleNamespace

    monkeypatch.setattr(session_start, "get_package_manager", lambda: SimpleNamespace(name=pm_name, source=pm_source))
    monkeypatch.setattr(session_start, "log", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "extract_coverage_hint_lines", lambda p: coverage)
    monkeypatch.chdir(tmp_path)


def test_collect_project_context_package_json_and_coverage(monkeypatch, tmp_path) -> None:
    """パッケージマネージャ未検出+package.json有+coverage hint有の経路。"""
    _stub_project_context(monkeypatch, tmp_path, coverage="- cov 100%")
    monkeypatch.setattr(session_start, "get_selection_prompt", lambda: "select")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    parts = session_start._collect_project_context(_pi(languages=["typescript"]))
    assert any("coverage_hint" in p for p in parts)


def test_collect_project_context_ruby(monkeypatch, tmp_path) -> None:
    """pm未検出+package.json無+ruby言語の経路。"""
    _stub_project_context(monkeypatch, tmp_path)
    session_start._collect_project_context(_pi(languages=["ruby"], frameworks=["rails"]))


def test_collect_project_context_other_language(monkeypatch, tmp_path) -> None:
    """pm未検出+package.json無+ruby以外の言語の経路。"""
    _stub_project_context(monkeypatch, tmp_path)
    session_start._collect_project_context(_pi(languages=["go"]))


def test_collect_project_context_frameworks_only(monkeypatch, tmp_path) -> None:
    """言語が空でフレームワークのみでもプロジェクト情報を出力する。"""
    _stub_project_context(monkeypatch, tmp_path, pm_name="npm", pm_source="x")
    parts = session_start._collect_project_context(_pi(languages=[], frameworks=["rails"]))
    assert any("Project type" in p for p in parts)
    assert any("rails" in p for p in parts)


def test_collect_project_context_languages_only(monkeypatch, tmp_path) -> None:
    """言語ありフレームワークなしでも Project type を出力する。"""
    _stub_project_context(monkeypatch, tmp_path, pm_name="pip", pm_source="x")
    parts = session_start._collect_project_context(_pi(languages=["python"], frameworks=[]))
    assert any("Project type" in p for p in parts)
    assert any("python" in p for p in parts)
