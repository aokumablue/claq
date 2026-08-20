"""launcher モジュール（インプロセス実行版）のテスト。"""

from __future__ import annotations

import json
import os
import runpy
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import bluecore.launcher as launcher


@pytest.fixture(autouse=True)
def _isolate_bluecore_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main() 経由の write_env_pointer が実 HOME の ~/.bluecore を汚さないようにする。

    main() は全 hook 起動で ~/.bluecore/env.sh 等を書き出す（M-02 対応）。
    get_home_dir() は BLUECORE_HOME を最優先で見るため、これだけ設定すれば
    このファイル内の main() 呼び出しはすべて tmp_path 配下へ書く。
    """
    monkeypatch.setenv("BLUECORE_HOME", str(tmp_path))


def _create_repo_venv(tmp_path: Path) -> Path:
    """無視されることを確認するため、仮の repo-local .venv を配置する。"""
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True, exist_ok=True)
    venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    venv_python.chmod(0o755)
    return venv_python


def _explode_if_called(*_args: object, **_kwargs: object) -> int:
    """バージョンガード通過前にターゲット実行へ進んだら失敗させる。"""
    raise AssertionError("target must not run")


def _raise_from_run_module(exc: BaseException) -> Callable[..., None]:
    """runpy.run_module の代わりに例外を送出するスタブを返す。"""

    def fake_run_module(
        _target: str, run_name: str | None = None, alter_sys: bool | None = None
    ) -> None:
        raise exc

    return fake_run_module


def _stub_run_in_process(monkeypatch: pytest.MonkeyPatch, returncode: int = 0) -> dict[str, object]:
    """_run_module_in_process を呼び出し記録用スタブに差し替える。"""
    captured: dict[str, object] = {}

    def fake_run_in_process(target: str, target_args: list[str]) -> int:
        captured["target"] = target
        captured["args"] = target_args
        return returncode

    monkeypatch.setattr(launcher, "_run_module_in_process", fake_run_in_process)
    return captured


class TestBuildEnv:
    """build_env が venv を見ず PYTHONPATH と CLAUDE_PLUGIN_ROOT だけを整えること。"""

    def test_planted_venv_does_not_set_virtual_env_or_prepend_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """仮に .venv があっても VIRTUAL_ENV を設定せず PATH も前置しない。"""
        _create_repo_venv(tmp_path)
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.setenv("PATH", "/usr/local/bin")
        monkeypatch.delenv("PYTHONPATH", raising=False)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        env = launcher.build_env()

        assert "VIRTUAL_ENV" not in env
        assert env["PATH"] == "/usr/local/bin"
        assert str(tmp_path / ".venv" / "bin") not in env["PATH"].split(os.pathsep)
        assert env["CLAUDE_PLUGIN_ROOT"] == str(tmp_path)
        assert env["PYTHONPATH"] == str(tmp_path / "src")

    def test_appends_existing_pythonpath(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """既存 PYTHONPATH は src の後ろに残す。"""
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.setenv("PYTHONPATH", "base-path")

        env = launcher.build_env()

        assert env["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "src"), "base-path"])

    def test_setdefault_claude_plugin_root(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """CLAUDE_PLUGIN_ROOT 未設定時は REPO_ROOT を入れる。"""
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)

        env = launcher.build_env()

        assert env["CLAUDE_PLUGIN_ROOT"] == str(tmp_path)

    def test_preserves_existing_claude_plugin_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既に CLAUDE_PLUGIN_ROOT がある場合は上書きしない。"""
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", "/already/set")

        env = launcher.build_env()

        assert env["CLAUDE_PLUGIN_ROOT"] == "/already/set"

    def test_without_path_env(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """PATH 未設定でも VIRTUAL_ENV を付けず動作する。"""
        _create_repo_venv(tmp_path)
        monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.delenv("VIRTUAL_ENV", raising=False)
        monkeypatch.delenv("PYTHONPATH", raising=False)

        env = launcher.build_env()

        assert "VIRTUAL_ENV" not in env
        assert "PATH" not in env
        assert env["PYTHONPATH"] == str(tmp_path / "src")
        assert env["CLAUDE_PLUGIN_ROOT"] == str(tmp_path)


class TestUnsupportedPythonExitCode:
    """Python 3.12 未満の fail-open 判定。"""

    def test_returns_none_on_312(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3.12.0 なら None を返す。"""
        monkeypatch.setattr(launcher.sys, "version_info", (3, 12, 0))

        assert launcher._unsupported_python_exit_code() is None

    @pytest.mark.parametrize("version", [(3, 9, 6), (3, 11, 9)])
    def test_returns_zero_on_old_python_and_writes_stderr(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        version: tuple[int, int, int],
    ) -> None:
        """3.12 未満なら stderr に実バージョンと構造化 JSON を書いて 0 を返す（F-07 対応）。"""
        monkeypatch.setattr(launcher.sys, "version_info", version)
        displayed = ".".join(str(part) for part in version)

        assert launcher._unsupported_python_exit_code() == 0
        err = capsys.readouterr().err
        lines = err.splitlines()
        assert lines[0] == (
            f"ERROR: bluecore requires Python 3.12+; `python3` is {displayed}. "
            "Point `python3` on PATH at 3.12+ (bluecore does not create a venv)."
        )
        payload = json.loads(lines[1])
        assert payload == {
            "bluecoreProtectionDisabled": True,
            "reason": "unsupported_python_version",
            "detectedVersion": displayed,
            "requiredVersion": "3.12+",
        }


class TestRunModuleInProcess:
    """runpy によるインプロセス実行の終了コード変換。"""

    def test_system_exit_zero_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SystemExit(0) は終了コード 0 にする。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", _raise_from_run_module(SystemExit(0)))

        assert launcher._run_module_in_process("bluecore.hooks.block_no_verify", []) == 0

    def test_system_exit_nonzero_returns_code(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SystemExit(非 0 整数) はそのコードを返す。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", _raise_from_run_module(SystemExit(2)))

        assert launcher._run_module_in_process("target", []) == 2

    def test_system_exit_none_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """SystemExit() は終了コード 0 にする。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", _raise_from_run_module(SystemExit()))

        assert launcher._run_module_in_process("target", []) == 0

    def test_system_exit_with_string_message_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """SystemExit(文字列) は stderr に書いて 1 を返す。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", _raise_from_run_module(SystemExit("bad")))

        assert launcher._run_module_in_process("target", []) == 1
        assert "bad" in capsys.readouterr().err

    def test_generic_exception_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """想定外例外は ERROR 行を書いて 1 を返す。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", _raise_from_run_module(RuntimeError("boom")))

        assert launcher._run_module_in_process("target", []) == 1
        assert "ERROR: target: boom" in capsys.readouterr().err

    def test_normal_completion_without_system_exit_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SystemExit なしの正常終了は 0 を返す。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        monkeypatch.setattr(launcher.runpy, "run_module", lambda *_a, **_k: None)

        assert launcher._run_module_in_process("target", []) == 0

    def test_sets_argv_from_target_and_args(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """sys.argv を target と引数で置き換えて run_module する。"""
        monkeypatch.setattr(sys, "argv", ["placeholder"])
        captured: dict[str, object] = {}

        def fake_run_module(target: str, run_name: str | None = None, alter_sys: bool | None = None) -> None:
            captured["argv"] = list(sys.argv)
            captured["run_name"] = run_name
            captured["alter_sys"] = alter_sys

        monkeypatch.setattr(launcher.runpy, "run_module", fake_run_module)

        launcher._run_module_in_process("bluecore.mem.cli", ["setup"])

        assert captured["argv"] == ["bluecore.mem.cli", "setup"]
        assert captured["run_name"] == "__main__"
        assert captured["alter_sys"] is True


class TestResolveModuleCommand:
    """detach 用コマンドリストの構築。"""

    def test_builds_module_invocation(self) -> None:
        """引数付きの python -m コマンドを返す。"""
        cmd = launcher._resolve_module_command("bluecore.hooks.config_protection", ["a", "b"])
        assert cmd == [sys.executable, "-m", "bluecore.hooks.config_protection", "a", "b"]

    def test_builds_module_invocation_without_args(self) -> None:
        """引数なしならモジュール名だけを付ける。"""
        cmd = launcher._resolve_module_command("bluecore.mem.cli", [])
        assert cmd == [sys.executable, "-m", "bluecore.mem.cli"]


class TestMain:
    """main() の引数処理・detach・バージョンガード。"""

    @pytest.fixture(autouse=True)
    def _assume_python_312(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main の 3.12 ガードを通過させる。古い版のテストは上書きする。"""
        monkeypatch.setattr(launcher.sys, "version_info", (3, 12, 0))

    def test_no_args_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """引数なしは usage を出して 1。"""
        assert launcher.main([]) == 1
        assert "Usage: python3" in capsys.readouterr().err

    def test_bg_without_target_prints_usage(self, capsys: pytest.CaptureFixture[str]) -> None:
        """--bg だけの指定は usage を出して 1。"""
        assert launcher.main(["--bg"]) == 1
        assert "Usage: python3" in capsys.readouterr().err

    def test_normal_invocation_runs_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """通常起動はインプロセス実行する。"""
        captured = _stub_run_in_process(monkeypatch)

        assert launcher.main(["bluecore.hooks.config_protection", "extra"]) == 0
        assert captured == {"target": "bluecore.hooks.config_protection", "args": ["extra"]}

    def test_bg_always_detaches_and_returns_zero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--bg は host に関わらず常に detach して 0 を返す。"""
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "{}")

        captured: dict[str, object] = {}

        def fake_detach(cmd: list[str], raw: str, *, env: dict[str, str] | None = None) -> bool:
            captured["cmd"] = cmd
            captured["raw"] = raw
            return True

        monkeypatch.setattr("bluecore.hooks.hook_common.detach_process", fake_detach)
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        run_in_process_called: list[object] = []
        monkeypatch.setattr(
            launcher, "_run_module_in_process", lambda *a: run_in_process_called.append(a)
        )

        assert launcher.main(["--bg", "bluecore.mem.cli", "handoff"]) == 0
        assert run_in_process_called == []
        assert captured["cmd"] == [sys.executable, "-m", "bluecore.mem.cli", "handoff"]
        assert captured["raw"] == "{}"

    def test_bg_detach_failure_still_returns_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """detach 失敗でも 0 を返し、stderr にエラーを書く。"""
        monkeypatch.setattr("bluecore.hooks.hook_common.read_raw_stdin", lambda: "")
        monkeypatch.setattr("bluecore.hooks.hook_common.detach_process", lambda *a, **k: False)
        monkeypatch.setattr(launcher, "build_env", lambda: {})

        assert launcher.main(["--bg", "bluecore.mem.cli", "handoff"]) == 0
        assert "Error detaching" in capsys.readouterr().err

    def test_inserts_src_dir_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """src ディレクトリが sys.path に無ければ挿入する。"""
        src = str(launcher.REPO_ROOT / "src")
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src])
        _stub_run_in_process(monkeypatch)

        assert launcher.main(["some.target"]) == 0
        assert src in sys.path

    def test_entrypoint_returns_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``python3 -m bluecore.launcher`` 相当は引数なしで usage 終了する。"""
        monkeypatch.setattr(sys, "argv", ["launcher.py"])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("bluecore.launcher", run_name="__main__")

        assert excinfo.value.code == 1

    @pytest.mark.parametrize("version", [(3, 9, 6), (3, 11, 9)])
    def test_old_python_returns_zero_without_running(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        version: tuple[int, int, int],
    ) -> None:
        """3.12 未満ではターゲットを実行せず 0 を返す。"""
        monkeypatch.setattr(launcher.sys, "version_info", version)
        monkeypatch.setattr(launcher, "_run_module_in_process", _explode_if_called)

        assert launcher.main(["bluecore.mem.cli", "context"]) == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "3.12" in captured.err
        assert ".".join(str(part) for part in version) in captured.err

    def test_python_312_runs_module_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """3.12.0 なら与えられたターゲットをインプロセス実行する。"""
        captured = _stub_run_in_process(monkeypatch)

        assert launcher.main(["bluecore.mem.cli", "context"]) == 0
        assert captured == {"target": "bluecore.mem.cli", "args": ["context"]}
