"""session_install フックのテスト。"""

from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bluecore.hooks import session_install


class TestGetPluginVersion:
    def test_reads_version_from_plugin_json(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"version": "1.2.3", "name": "bluecore"}))

        assert session_install._get_plugin_version(tmp_path) == "1.2.3"

    def test_returns_none_when_version_missing_from_json(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"name": "bluecore"}))

        assert session_install._get_plugin_version(tmp_path) is None

    def test_returns_none_when_file_not_found(self, tmp_path: Path) -> None:
        assert session_install._get_plugin_version(tmp_path) is None

    def test_returns_none_when_invalid_json(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text("not-json")

        result = session_install._get_plugin_version(tmp_path)
        assert result is None
        assert "plugin.json" in capsys.readouterr().err


class TestResolvePluginRoot:
    def test_returns_none_when_env_missing(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        assert session_install._resolve_plugin_root() is None
        assert "CLAUDE_PLUGIN_ROOT" in capsys.readouterr().err

    def test_returns_none_when_plugin_json_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", tmp_path)

        assert session_install._resolve_plugin_root() is None
        assert "不正なプラグインルート" in capsys.readouterr().err

    def test_returns_none_when_root_mismatch(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        other_root = tmp_path / "other"
        other_root.mkdir()
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(other_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", tmp_path)

        assert session_install._resolve_plugin_root() is None
        assert "不正なプラグインルート" in capsys.readouterr().err

    def test_returns_resolved_root_when_valid(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"version": "1.0.0"}))
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", tmp_path)

        assert session_install._resolve_plugin_root() == tmp_path.resolve()


class TestGetInstalledVersion:
    def test_returns_none_when_file_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_install, "_VERSION_FILE", tmp_path / "plugin_installed_version")

        assert session_install._get_installed_version() is None

    def test_reads_version_from_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)

        assert session_install._get_installed_version() == "0.0.2"

    def test_strips_whitespace(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("  0.0.3  \n")
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)

        assert session_install._get_installed_version() == "0.0.3"


def _make_plugin_root(tmp_path: Path, version: str) -> Path:
    """plugin.json を持つ偽プラグインルートを作成する。"""
    plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
    plugin_json.parent.mkdir(parents=True)
    plugin_json.write_text(json.dumps({"version": version}))
    return tmp_path


def _assert_session_start_output(result: str) -> None:
    payload = json.loads(result)
    assert payload == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }


class TestRun:
    def test_skips_when_no_claude_plugin_root(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        result = json.loads(session_install.run(""))

        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }
        assert "CLAUDE_PLUGIN_ROOT" in capsys.readouterr().err

    def test_skips_when_plugin_json_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", tmp_path)

        result = json.loads(session_install.run(""))

        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }
        assert "不正なプラグインルート" in capsys.readouterr().err

    def test_skips_when_version_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plugin_root = _make_plugin_root(tmp_path, "0.0.2")
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)

        result = json.loads(session_install.run(""))

        assert result == {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "",
            }
        }

    @pytest.mark.parametrize(
        ("current_version", "installed_version"),
        [
            ("0.0.2", "0.0.2\n"),
            ("1.2.3", "1.2.3\n"),
        ],
    )
    def test_version_matched_skips_install_table_driven(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        current_version: str,
        installed_version: str,
    ) -> None:
        plugin_root = _make_plugin_root(tmp_path, current_version)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text(installed_version)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)

        result = session_install.run("")
        _assert_session_start_output(result)

    def test_runs_install_when_no_version_file(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """plugin_installed_version が無い初回起動時は install.sh を実行する。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.2")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"  # 存在しない
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result) as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_called_once()

    def test_runs_install_when_version_changed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """バージョン不一致時は install.sh を実行する。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result) as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_called_once()

    def test_run_install_does_not_pass_extra_env(self, tmp_path: Path) -> None:
        """_run_install が extra_env なしで run_text を呼ぶこと（モデル取得は同期実行）。"""
        install_sh = tmp_path / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)

        captured_kwargs: dict = {}

        def fake_run_text(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
            captured_kwargs.update(kwargs)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch.object(session_install, "run_text", side_effect=fake_run_text):
            session_install._run_install(install_sh)

        assert "extra_env" not in captured_kwargs

    def test_install_stdout_stderr_routed_to_stderr(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """install.sh の stdout/stderr が SessionInstall の stderr に出力される。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = "install stdout line"
        fake_result.stderr = "install stderr line"
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result):
            session_install.run("")

        err = capsys.readouterr().err
        assert "install stdout line" in err
        assert "install stderr line" in err

    def test_skips_install_when_install_sh_escapes_plugin_root(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """install.sh がプラグインルート外を指す場合はスキップする。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        # install.sh はプラグインルート内に置かない
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

        with patch.object(session_install, "_run_install") as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_not_called()
        assert "install.sh" in capsys.readouterr().err

    def test_install_failure_emits_session_start(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """install.sh が非ゼロ終了しても SessionStart JSON を返し、モデル未取得通知は出さない。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = "install failed"
        fake_result.returncode = 1

        with patch.object(session_install, "_run_install", return_value=fake_result):
            result = session_install.run("")

        _assert_session_start_output(result)
        err = capsys.readouterr().err
        assert "埋め込みモデルが未取得です" not in err, "モデル未取得通知が出てはいけない"
        assert "失敗" in err

    def test_lock_phase_recheck_skips_when_other_process_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """ロック取得後に別プロセスが既にインストールしていればスキップする。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        # ロック取得前には古いバージョン、ロック取得後には新バージョンに切り替える
        call_count = 0

        def fake_get_installed() -> str | None:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "0.0.2"  # ロック取得前: 旧バージョン（install が走る判定）
            return "0.0.3"  # ロック取得後: 新バージョン（スキップ判定）

        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)
        monkeypatch.setattr(session_install, "_get_installed_version", fake_get_installed)

        with patch.object(session_install, "_run_install") as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_not_called()
        assert "別プロセス" in capsys.readouterr().err


class TestMain:
    def test_returns_zero_on_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        assert session_install.main() == 0

    def test_returns_zero_on_exception(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr(session_install, "run", lambda _: (_ for _ in ()).throw(RuntimeError("boom")))

        assert session_install.main() == 0

    def test_reads_stdin_when_not_tty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        assert session_install.main() == 0

    def test_main_block_via_runpy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            runpy.run_module("bluecore.hooks.session_install", run_name="__main__")

        assert exc_info.value.code == 0


import subprocess as _subprocess  # noqa: E402


def _raise(exc):
    def _inner(*a, **k):
        raise exc
    return _inner


def test_lock_phase_version_read_error(tmp_path, monkeypatch, capsys) -> None:
    """インストール済みバージョン読込失敗ならスキップする。"""
    monkeypatch.setattr(session_install, "_get_installed_version", _raise(OSError()))
    assert session_install._lock_phase_should_skip(tmp_path, "1.0") is True
    assert "読み込みに失敗" in capsys.readouterr().err


def test_lock_phase_already_installed_skips_without_repair(tmp_path, monkeypatch) -> None:
    """既に同一バージョンなら修復せずスキップする。"""
    monkeypatch.setattr(session_install, "_get_installed_version", lambda: "1.0")
    assert session_install._lock_phase_should_skip(tmp_path, "1.0") is True


def test_run_install_subprocess_error(tmp_path, monkeypatch) -> None:
    """install.sh 実行失敗時は None を返す。"""
    monkeypatch.setattr(session_install, "run_text", _raise(_subprocess.SubprocessError()))
    assert session_install._run_install(tmp_path / "install.sh") is None


def test_run_install_with_lock_result_none(tmp_path, monkeypatch) -> None:
    """install 実行が None を返せば False。"""
    from contextlib import contextmanager

    @contextmanager
    def fake_lock(p):
        yield

    monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)
    monkeypatch.setattr(session_install, "install_lock", fake_lock)
    monkeypatch.setattr(session_install, "_lock_phase_should_skip", lambda r, v: False)
    monkeypatch.setattr(session_install, "_precheck_install_target", lambda r: tmp_path / "install.sh")
    monkeypatch.setattr(session_install, "_run_install", lambda s: None)
    assert session_install._run_install_with_lock(tmp_path, "1.0") is False


def test_run_install_with_lock_oserror(tmp_path, monkeypatch, capsys) -> None:
    """ロック取得が OSError なら False。"""
    monkeypatch.setattr(session_install, "_BLUECORE_DIR", tmp_path)
    monkeypatch.setattr(session_install, "install_lock", _raise(OSError("lock")))
    assert session_install._run_install_with_lock(tmp_path, "1.0") is False
    assert "ロック取得失敗" in capsys.readouterr().err


def test_lock_phase_already_installed_no_repair(tmp_path, monkeypatch) -> None:
    """同一バージョンならそのままスキップする。"""
    monkeypatch.setattr(session_install, "_get_installed_version", lambda: "1.0")
    assert session_install._lock_phase_should_skip(tmp_path, "1.0") is True


def test_run_success_without_repair(tmp_path, monkeypatch) -> None:
    """インストール成功後に追加修復せず SessionStart JSON を返す。"""
    monkeypatch.setattr(session_install, "_resolve_plugin_root", lambda: tmp_path)
    monkeypatch.setattr(session_install, "_get_plugin_version", lambda r: "2.0")
    monkeypatch.setattr(session_install, "_get_installed_version", lambda: "1.0")
    monkeypatch.setattr(session_install, "_run_install_with_lock", lambda r, v: True)
    out = session_install.run("")
    assert isinstance(out, str)


def test_run_skips_grok_log_when_symlink_unchanged(monkeypatch, capsys) -> None:
    """Grok plugin-root が既に揃っている（戻り値 None）ときは追加ログしない。"""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with patch(
        "bluecore.lib.grok_plugin_root.ensure_grok_plugin_root_symlink",
        return_value=None,
    ):
        out = session_install.run("")
    assert isinstance(out, str)
    assert "Grok plugin root symlink" not in capsys.readouterr().err


def test_run_logs_grok_plugin_root_symlink(tmp_path, monkeypatch, capsys) -> None:
    """Grok plugin root symlink を張ったときはログする。"""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with patch(
        "bluecore.lib.grok_plugin_root.ensure_grok_plugin_root_symlink",
        return_value=tmp_path,
    ):
        out = session_install.run("")
    assert isinstance(out, str)
    assert "Grok plugin root symlink" in capsys.readouterr().err


def test_run_grok_symlink_exception_is_fail_open(monkeypatch, capsys) -> None:
    """Grok symlink 修復の例外でセッション開始を止めない。"""
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    with patch(
        "bluecore.lib.grok_plugin_root.ensure_grok_plugin_root_symlink",
        side_effect=OSError("x"),
    ):
        out = session_install.run("")
    assert isinstance(out, str)
    assert "Grok symlink 修復スキップ" in capsys.readouterr().err


def test_install_timeout_below_hooks_json_limit() -> None:
    """hooks.json timeout 60 より先に自決する。"""
    assert session_install._INSTALL_TIMEOUT < 60
