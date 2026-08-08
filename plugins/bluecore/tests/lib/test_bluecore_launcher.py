"""launcher モジュール（インプロセス実行版）のテスト。"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

import pytest

import bluecore.launcher as launcher


def _create_repo_venv(tmp_path: Path) -> Path:
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_python.chmod(0o755)
    return venv_python


class TestRuntimePython:
    def test_prefers_repo_venv_python(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        venv_python = _create_repo_venv(tmp_path)
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

        python, venv_root = launcher._runtime_python()

        assert python == str(venv_python)
        assert venv_root == tmp_path / ".venv"

    def test_falls_back_to_system_python_without_repo_venv(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)

        python, venv_root = launcher._runtime_python()

        assert python == sys.executable
        assert venv_root is None


class TestBuildEnv:
    def test_prepends_repo_venv_to_path(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _create_repo_venv(tmp_path)
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.setenv("PATH", "/usr/local/bin")
        monkeypatch.delenv("PYTHONPATH", raising=False)

        env = launcher.build_env()

        assert env["CLAUDE_PLUGIN_ROOT"] == str(tmp_path)
        assert env["VIRTUAL_ENV"] == str(tmp_path / ".venv")
        assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / ".venv" / "bin")

    def test_appends_existing_pythonpath(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (sys.executable, None))
        monkeypatch.setenv("PYTHONPATH", "base-path")

        env = launcher.build_env()

        assert env["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "src"), "base-path"])

    def test_without_path_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """PATH 未設定でも venv の PATH を構築する。"""
        monkeypatch.setattr(launcher, "_runtime_python", lambda: ("py", tmp_path))
        monkeypatch.delenv("PATH", raising=False)

        env = launcher.build_env()

        assert env["VIRTUAL_ENV"] == str(tmp_path)
        assert str(tmp_path / "bin") in env["PATH"]


class TestReexecIntoVenvIfNeeded:
    """os.execve による venv 自己置換の判定ロジックのテスト。

    venv の python3 実行ファイルは多くの場合ベースインタプリタへの symlink
    のため、単純な実体比較（samefile）では venv 有効化の要否を誤判定する
    （NG 回帰）。sys.prefix != sys.base_prefix を主判定に使うことを確認する。
    """

    def test_no_venv_skips_exec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (sys.executable, None))
        called = []
        monkeypatch.setattr(launcher.os, "execve", lambda *a: called.append(a))

        launcher._reexec_into_venv_if_needed()

        assert called == []

    def test_bare_system_python_always_execs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """sys.prefix == sys.base_prefix（venv 非活性）なら、venv python3 が
        システム python への symlink であっても必ず exec する。"""
        venv_python = _create_repo_venv(tmp_path)
        venv_root = tmp_path / ".venv"
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (str(venv_python), venv_root))
        monkeypatch.setattr(launcher.sys, "prefix", "/usr")
        monkeypatch.setattr(launcher.sys, "base_prefix", "/usr")
        # samefile が True（symlink 先が同じ実体）でも exec すべきことを保証するため、
        # わざと同一ファイルとの比較で True を返すよう仕込む。
        monkeypatch.setattr(launcher.os.path, "samefile", lambda a, b: True)
        called = []
        monkeypatch.setattr(launcher.os, "execve", lambda *a: called.append(a))
        monkeypatch.setattr(launcher, "build_env", lambda: {"X": "1"})

        launcher._reexec_into_venv_if_needed()

        assert len(called) == 1
        assert called[0][0] == str(venv_python)

    def test_already_in_matching_venv_skips_exec(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        venv_python = _create_repo_venv(tmp_path)
        venv_root = tmp_path / ".venv"
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (str(venv_python), venv_root))
        monkeypatch.setattr(launcher.sys, "prefix", str(venv_root))
        monkeypatch.setattr(launcher.sys, "base_prefix", "/usr")
        monkeypatch.setattr(launcher.os.path, "samefile", lambda a, b: True)
        called = []
        monkeypatch.setattr(launcher.os, "execve", lambda *a: called.append(a))

        launcher._reexec_into_venv_if_needed()

        assert called == []

    def test_in_different_venv_still_execs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        venv_python = _create_repo_venv(tmp_path)
        venv_root = tmp_path / ".venv"
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (str(venv_python), venv_root))
        monkeypatch.setattr(launcher.sys, "prefix", "/some/other/venv")
        monkeypatch.setattr(launcher.sys, "base_prefix", "/usr")
        monkeypatch.setattr(launcher.os.path, "samefile", lambda a, b: False)
        called = []
        monkeypatch.setattr(launcher.os, "execve", lambda *a: called.append(a))
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        launcher._reexec_into_venv_if_needed()

        assert len(called) == 1

    def test_samefile_oserror_falls_through_to_exec(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        venv_python = _create_repo_venv(tmp_path)
        venv_root = tmp_path / ".venv"
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (str(venv_python), venv_root))
        monkeypatch.setattr(launcher.sys, "prefix", str(venv_root))
        monkeypatch.setattr(launcher.sys, "base_prefix", "/usr")

        def fail_samefile(a, b):  # noqa: ANN001
            raise OSError("boom")

        monkeypatch.setattr(launcher.os.path, "samefile", fail_samefile)
        called = []
        monkeypatch.setattr(launcher.os, "execve", lambda *a: called.append(a))
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        launcher._reexec_into_venv_if_needed()

        assert len(called) == 1

    def test_execve_oserror_is_swallowed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        venv_python = _create_repo_venv(tmp_path)
        venv_root = tmp_path / ".venv"
        monkeypatch.setattr(launcher, "_runtime_python", lambda: (str(venv_python), venv_root))
        monkeypatch.setattr(launcher.sys, "prefix", "/usr")
        monkeypatch.setattr(launcher.sys, "base_prefix", "/usr")
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        def fail_execve(*a):
            raise OSError("exec failed")

        monkeypatch.setattr(launcher.os, "execve", fail_execve)

        # 例外を送出せず戻ってくることを確認する（fail-open）。
        launcher._reexec_into_venv_if_needed()


class TestRunModuleInProcess:
    def test_system_exit_zero_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])

        def fake_run_module(target, run_name=None, alter_sys=None):  # noqa: ANN001
            raise SystemExit(0)

        monkeypatch.setattr(launcher.runpy, "run_module", fake_run_module)

        assert launcher._run_module_in_process("bluecore.hooks.block_no_verify", []) == 0

    def test_system_exit_nonzero_returns_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(
            launcher.runpy, "run_module", lambda *a, **k: (_ for _ in ()).throw(SystemExit(2))
        )

        assert launcher._run_module_in_process("target", []) == 2

    def test_system_exit_none_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(
            launcher.runpy, "run_module", lambda *a, **k: (_ for _ in ()).throw(SystemExit())
        )

        assert launcher._run_module_in_process("target", []) == 0

    def test_system_exit_with_string_message_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(
            launcher.runpy, "run_module", lambda *a, **k: (_ for _ in ()).throw(SystemExit("bad"))
        )

        assert launcher._run_module_in_process("target", []) == 1
        assert "bad" in capsys.readouterr().err

    def test_generic_exception_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(
            launcher.runpy, "run_module", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )

        assert launcher._run_module_in_process("target", []) == 1
        assert "ERROR: target: boom" in capsys.readouterr().err

    def test_normal_completion_without_system_exit_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", lambda *a, **k: None)

        assert launcher._run_module_in_process("target", []) == 0

    def test_sets_argv_from_target_and_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        captured = {}

        def fake_run_module(target, run_name=None, alter_sys=None):  # noqa: ANN001
            captured["argv"] = list(sys.argv)
            captured["run_name"] = run_name
            captured["alter_sys"] = alter_sys

        monkeypatch.setattr(launcher.runpy, "run_module", fake_run_module)

        launcher._run_module_in_process("bluecore.mem.cli", ["setup"])

        assert captured["argv"] == ["bluecore.mem.cli", "setup"]
        assert captured["run_name"] == "__main__"
        assert captured["alter_sys"] is True


class TestResolveModuleCommand:
    def test_builds_module_invocation(self) -> None:
        cmd = launcher._resolve_module_command("bluecore.hooks.config_protection", ["a", "b"])
        assert cmd == [sys.executable, "-m", "bluecore.hooks.config_protection", "a", "b"]

    def test_builds_module_invocation_without_args(self) -> None:
        cmd = launcher._resolve_module_command("bluecore.hooks.session_start", [])
        assert cmd == [sys.executable, "-m", "bluecore.hooks.session_start"]


class TestMain:
    def _quiet_reexec(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """テストでは実際の execve を発火させない。"""
        monkeypatch.setattr(launcher, "_reexec_into_venv_if_needed", lambda: None)

    def test_no_args_prints_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._quiet_reexec(monkeypatch)

        assert launcher.main([]) == 1
        assert "Usage: python3" in capsys.readouterr().err

    def test_bg_without_target_prints_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._quiet_reexec(monkeypatch)

        assert launcher.main(["--bg"]) == 1
        assert "Usage: python3" in capsys.readouterr().err

    def test_normal_invocation_runs_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._quiet_reexec(monkeypatch)
        captured = {}

        def fake_run_in_process(target, target_args):  # noqa: ANN001
            captured["target"] = target
            captured["args"] = target_args
            return 0

        monkeypatch.setattr(launcher, "_run_module_in_process", fake_run_in_process)

        assert launcher.main(["bluecore.hooks.config_protection", "extra"]) == 0
        assert captured == {"target": "bluecore.hooks.config_protection", "args": ["extra"]}

    def test_bg_on_claude_runs_in_process_without_detach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Claude はホスト側で非同期実行するため、--bg でも detach せずインプロセス実行する。"""
        self._quiet_reexec(monkeypatch)
        monkeypatch.setattr("bluecore.lib.harness.detect_harness", lambda: "claude")

        detach_called = []
        monkeypatch.setattr("bluecore.hooks.hook_common.detach_process", lambda *a, **k: detach_called.append(a) or True)

        captured = {}

        def fake_run_in_process(target, args):  # noqa: ANN001
            captured["target"] = target
            return 0

        monkeypatch.setattr(launcher, "_run_module_in_process", fake_run_in_process)

        assert launcher.main(["--bg", "bluecore.mem.cli", "observe"]) == 0
        assert detach_called == []
        assert captured["target"] == "bluecore.mem.cli"

    def test_bg_on_non_claude_detaches_and_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self._quiet_reexec(monkeypatch)
        monkeypatch.setattr("bluecore.lib.harness.detect_harness", lambda: "codex")
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")

        captured = {}

        def fake_detach(cmd, raw, *, env=None):  # noqa: ANN001
            captured["cmd"] = cmd
            captured["raw"] = raw
            return True

        monkeypatch.setattr("bluecore.hooks.hook_common.detach_process", fake_detach)
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        run_in_process_called = []
        monkeypatch.setattr(
            launcher, "_run_module_in_process", lambda *a: run_in_process_called.append(a)
        )

        assert launcher.main(["--bg", "bluecore.mem.cli", "session-end"]) == 0
        assert run_in_process_called == []
        assert captured["cmd"] == [sys.executable, "-m", "bluecore.mem.cli", "session-end"]
        assert captured["raw"] == "{}"

    def test_bg_on_non_claude_detach_failure_still_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        self._quiet_reexec(monkeypatch)
        monkeypatch.setattr("bluecore.lib.harness.detect_harness", lambda: "codex")
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "")
        monkeypatch.setattr("bluecore.hooks.hook_common.detach_process", lambda *a, **k: False)
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        assert launcher.main(["--bg", "bluecore.hooks.session_end"]) == 0
        assert "Error detaching" in capsys.readouterr().err

    def test_inserts_src_dir_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """src ディレクトリが sys.path に無ければ挿入する。"""
        self._quiet_reexec(monkeypatch)
        src = str(launcher.REPO_ROOT / "src")
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src])
        monkeypatch.setattr(launcher, "_run_module_in_process", lambda target, args: 0)

        assert launcher.main(["some.target"]) == 0
        assert src in sys.path

    def test_entrypoint_returns_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # runpy.run_module は launcher モジュールを "__main__" として再実行するため、
        # 既存の launcher オブジェクトへの monkeypatch は effective ではない。
        # os.execve をグローバルに封じることで、テスト環境のプロセスが本当に
        # 自己置換されてしまう事故を防ぐ（_reexec_into_venv_if_needed は OSError
        # を握りつぶして続行する設計のため安全に空振りできる）。
        def blocked_execve(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            raise OSError("execve blocked in test")

        monkeypatch.setattr(os, "execve", blocked_execve)
        monkeypatch.setattr(sys, "argv", ["launcher.py"])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.launcher", run_name="__main__")

        assert excinfo.value.code == 1
