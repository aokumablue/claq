"""session_install フックのテスト。"""

from __future__ import annotations

import json
import runpy
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from devgear.hooks import session_install


class TestGetPluginVersion:
    def test_reads_version_from_plugin_json(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"version": "1.2.3", "name": "devgear"}))

        assert session_install._get_plugin_version(tmp_path) == "1.2.3"

    def test_returns_none_when_version_missing_from_json(self, tmp_path: Path) -> None:
        plugin_json = tmp_path / ".claude-plugin" / "plugin.json"
        plugin_json.parent.mkdir()
        plugin_json.write_text(json.dumps({"name": "devgear"}))

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
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)

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
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result) as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_called_once()

    def test_run_install_passes_onnx_async_env(self, tmp_path: Path) -> None:
        """_run_install が DEVGEAR_INSTALL_ONNX_ASYNC=1 で run_text を呼ぶこと。"""
        install_sh = tmp_path / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)

        captured_extra_env: dict[str, str] = {}

        def fake_run_text(cmd: list[str], *, timeout: float | None, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
            if extra_env:
                captured_extra_env.update(extra_env)
            return MagicMock(stdout="", stderr="", returncode=0)

        with patch.object(session_install, "run_text", side_effect=fake_run_text):
            session_install._run_install(install_sh)

        assert captured_extra_env.get("DEVGEAR_INSTALL_ONNX_ASYNC") == "1"

    def test_warns_onnx_building_when_model_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """install 成功後 model.onnx が無いなら 'onnx building...' を出力する。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)
        # model.onnx が存在しない状態をシミュレート
        model_onnx = tmp_path / "models" / "model.onnx"
        monkeypatch.setattr(session_install.Path, "home", lambda: tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result):
            session_install.run("")

        err = capsys.readouterr().err
        assert "onnx building..." in err
        assert model_onnx.exists() is False  # model.onnx はまだない

    def test_does_not_warn_onnx_building_when_model_present(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """model.onnx が存在するなら 'onnx building...' を出力しない。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)
        # model.onnx が存在する状態をシミュレート
        model_dir = tmp_path / ".devgear" / "models"
        model_dir.mkdir(parents=True)
        (model_dir / "model.onnx").write_bytes(b"")
        monkeypatch.setattr(session_install.Path, "home", lambda: tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = ""
        fake_result.returncode = 0

        with patch.object(session_install, "_run_install", return_value=fake_result):
            session_install.run("")

        err = capsys.readouterr().err
        assert "onnx building..." not in err

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
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)

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
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)

        with patch.object(session_install, "_run_install") as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_not_called()
        assert "install.sh" in capsys.readouterr().err

    def test_install_failure_skips_venv_symlink_repair(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """install.sh が非ゼロ終了したとき .venv symlink 修復と onnx building 通知はスキップされる。"""
        plugin_root = _make_plugin_root(tmp_path, "0.0.3")
        install_sh = plugin_root / "install.sh"
        install_sh.write_text("#!/usr/bin/env bash\n")
        install_sh.chmod(0o755)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("0.0.2\n")
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))
        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)
        monkeypatch.setattr(session_install.Path, "home", lambda: tmp_path)

        fake_result = MagicMock(spec=subprocess.CompletedProcess)
        fake_result.stdout = ""
        fake_result.stderr = "install failed"
        fake_result.returncode = 1

        repair_called = False

        def mock_repair(_: object) -> None:
            nonlocal repair_called
            repair_called = True

        with (
            patch.object(session_install, "_run_install", return_value=fake_result),
            patch.object(session_install, "_repair_venv_symlink", side_effect=mock_repair),
        ):
            result = session_install.run("")

        _assert_session_start_output(result)
        assert repair_called is False, ".venv symlink 修復が実行されてはいけない"
        err = capsys.readouterr().err
        assert "onnx building..." not in err, "onnx building 通知が出てはいけない"
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
        monkeypatch.setattr(session_install, "_DEVGEAR_DIR", tmp_path)
        monkeypatch.setattr(session_install, "_get_installed_version", fake_get_installed)

        with patch.object(session_install, "_run_install") as mock_run:
            result = session_install.run("")

        _assert_session_start_output(result)
        mock_run.assert_not_called()
        assert "別プロセス" in capsys.readouterr().err


class TestShouldRepairVenvSymlink:
    def test_returns_false_when_shared_venv_absent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(session_install, "_VENV_DIR", tmp_path / "nonexistent")
        assert session_install._should_repair_venv_symlink(tmp_path) is False

    def test_returns_true_when_symlink_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        # .venv が存在しない
        assert session_install._should_repair_venv_symlink(plugin_root) is True

    def test_returns_false_when_correct_symlink_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        (plugin_root / ".venv").symlink_to(shared_venv, target_is_directory=True)
        assert session_install._should_repair_venv_symlink(plugin_root) is False

    def test_returns_true_when_symlink_points_elsewhere(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        other = tmp_path / "other"
        other.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        (plugin_root / ".venv").symlink_to(other, target_is_directory=True)
        assert session_install._should_repair_venv_symlink(plugin_root) is True

    def test_returns_true_when_real_directory_exists(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        (plugin_root / ".venv").mkdir()  # 実体ディレクトリ
        assert session_install._should_repair_venv_symlink(plugin_root) is True


class TestRepairVenvSymlink:
    def test_creates_symlink_when_missing(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()

        session_install._repair_venv_symlink(plugin_root)

        link = plugin_root / ".venv"
        assert link.is_symlink()
        assert link.resolve() == shared_venv.resolve()

    def test_replaces_broken_symlink(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        (plugin_root / ".venv").symlink_to(tmp_path / "nonexistent")  # 破損 symlink

        session_install._repair_venv_symlink(plugin_root)

        link = plugin_root / ".venv"
        assert link.is_symlink()
        assert link.resolve() == shared_venv.resolve()

    def test_does_not_touch_real_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        plugin_root = tmp_path / "plugin"
        plugin_root.mkdir()
        real_dir = plugin_root / ".venv"
        real_dir.mkdir()

        session_install._repair_venv_symlink(plugin_root)

        assert real_dir.is_dir() and not real_dir.is_symlink()
        assert "自動修復を中止" in capsys.readouterr().err


class TestRepairVenvSymlinkIntegration:
    """version 一致時の symlink 修復が run() を通じて動作することを確認する。"""

    def test_run_repairs_missing_symlink_on_version_match(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        plugin_root = _make_plugin_root(tmp_path / "plugin", "1.0.0")
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("1.0.0\n")

        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        result = json.loads(session_install.run(""))

        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        link = plugin_root / ".venv"
        assert link.is_symlink()
        assert link.resolve() == shared_venv.resolve()

    def test_run_skips_repair_when_symlink_correct(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        shared_venv = tmp_path / "shared_venv"
        shared_venv.mkdir()
        plugin_root = _make_plugin_root(tmp_path / "plugin", "1.0.0")
        (plugin_root / ".venv").symlink_to(shared_venv, target_is_directory=True)
        version_file = tmp_path / "plugin_installed_version"
        version_file.write_text("1.0.0\n")

        monkeypatch.setattr(session_install, "_PLUGIN_ROOT", plugin_root)
        monkeypatch.setattr(session_install, "_VENV_DIR", shared_venv)
        monkeypatch.setattr(session_install, "_VERSION_FILE", version_file)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(plugin_root))

        mtime_before = (plugin_root / ".venv").lstat().st_mtime
        result = json.loads(session_install.run(""))

        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert (plugin_root / ".venv").lstat().st_mtime == mtime_before  # 変更されていない


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
            runpy.run_module("devgear.hooks.session_install", run_name="__main__")

        assert exc_info.value.code == 0
