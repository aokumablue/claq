"""launcher モジュール（インプロセス実行版）のテスト。"""

from __future__ import annotations

import json
import os
import runpy
import subprocess
import sys
import time
from pathlib import Path

import pytest

import claq.launcher as launcher

LAUNCHER_PATH = Path(launcher.__file__).resolve()


@pytest.fixture(autouse=True)
def _isolate_claq_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """main() が全 hook 起動で書く ~/.claq/env.sh 等を、実 HOME ではなく tmp へ向ける（state dir は ``$HOME`` 固定）。"""
    monkeypatch.setenv("HOME", str(tmp_path))


def _stub_run_in_process(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """_run_module_in_process を呼び出し記録用スタブ（終了コード 0）に差し替える。"""
    captured: dict[str, object] = {}

    def fake_run_in_process(target: str, target_args: list[str]) -> int:
        captured["target"] = target
        captured["args"] = target_args
        return 0

    monkeypatch.setattr(launcher, "_run_module_in_process", fake_run_in_process)
    return captured


def _non_utf8_env(home: Path) -> dict[str, str]:
    """ホスト固有の変数を除き、Windows の既定コードページを POSIX で近似した非 UTF-8 ロケールの環境を返す。"""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("CODEX_", "GROK_", "COPILOT_"))
        and key not in {"CLAUDECODE", "CLAQ_HOME", "CLAUDE_PLUGIN_ROOT"}
    }
    env.update(
        {"HOME": str(home), "CLAQ_DATA_PATH": str(home), "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    )
    return env


@pytest.mark.parametrize(
    ("path", "pythonpath", "plugin_root"),
    [
        ("/usr/local/bin", None, None),
        ("/usr/local/bin", "base-path", None),
        ("/usr/local/bin", None, "/already/set"),
        (None, None, None),
    ],
    ids=["defaults", "appends-existing-pythonpath", "keeps-existing-plugin-root", "without-path"],
)
def test_build_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str | None, pythonpath: str | None, plugin_root: str | None
) -> None:
    """build_env は repo-local .venv を見ず、PYTHONPATH・CLAUDE_PLUGIN_ROOT・PYTHONIOENCODING だけを整える。

    PYTHONIOENCODING は detach 子（`-m <module>` 起動で force_utf8_streams を通らない）の日本語警告を
    非 UTF-8 ロケールの bg ログで壊さないため。errors を ``strict`` にするとサロゲートで子だけが落ちる。
    """
    venv_python = tmp_path / ".venv" / "bin" / "python3"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    monkeypatch.setattr(launcher, "REPO_ROOT", tmp_path)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    for name, value in (("PATH", path), ("PYTHONPATH", pythonpath), ("CLAUDE_PLUGIN_ROOT", plugin_root)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)

    env = launcher.build_env()

    assert "VIRTUAL_ENV" not in env
    assert env.get("PATH") == path
    assert env["PYTHONPATH"] == os.pathsep.join([str(tmp_path / "src"), *filter(None, [pythonpath])])
    assert env["CLAUDE_PLUGIN_ROOT"] == (plugin_root or str(tmp_path))
    assert env["PYTHONIOENCODING"] == "utf-8:replace"


class TestUnsupportedPythonExitCode:
    """Python 3.12 未満の fail-open 判定。"""

    def test_returns_none_on_312(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        """3.12.0 なら何も書かず None を返す。"""
        monkeypatch.setattr(launcher.sys, "version_info", (3, 12, 0))

        assert launcher._unsupported_python_exit_code() is None
        assert capsys.readouterr().err == ""

    @pytest.mark.parametrize("version", [(3, 9, 6), (3, 11, 9)])
    def test_returns_zero_on_old_python_and_writes_stderr(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: tuple[int, int, int]
    ) -> None:
        """3.12 未満なら stderr に実バージョンと構造化 JSON を書いて 0 を返す。"""
        monkeypatch.setattr(launcher.sys, "version_info", version)
        displayed = ".".join(str(part) for part in version)

        assert launcher._unsupported_python_exit_code() == 0
        lines = capsys.readouterr().err.splitlines()
        assert lines[0] == (
            f"ERROR: claq requires Python 3.12+; `python3` is {displayed}. "
            "Point `python3` on PATH at 3.12+ (claq does not create a venv)."
        )
        assert json.loads(lines[1]) == {
            "claqProtectionDisabled": True,
            "reason": "unsupported_python_version",
            "detectedVersion": displayed,
            "requiredVersion": "3.12+",
        }


@pytest.mark.parametrize(
    ("raised", "expected_code", "expected_err"),
    [
        (None, 0, ""),
        (SystemExit(0), 0, ""),
        (SystemExit(), 0, ""),
        (SystemExit(2), 2, ""),
        (SystemExit("bad"), 1, "bad\n"),
        (
            RuntimeError("boom"),
            1,
            'ERROR: target: boom\n{"claqProtectionDisabled": true, "reason": "hook_raised", "module": "target"}\n',
        ),
    ],
    ids=["normal-completion", "exit-0", "exit-none", "exit-2", "exit-message", "unexpected-exception"],
)
def test_run_module_in_process(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raised: BaseException | None,
    expected_code: int,
    expected_err: str,
) -> None:
    """sys.argv を target と引数へ差し替えて __main__ として runpy し、終了の仕方を終了コードへ変換する。"""
    monkeypatch.setattr(sys, "argv", ["placeholder"])
    calls: list[tuple[object, ...]] = []

    def fake_run_module(target: str, run_name: str | None = None, alter_sys: bool | None = None) -> None:
        calls.append((target, list(sys.argv), run_name, alter_sys))
        if raised is not None:
            raise raised

    monkeypatch.setattr(launcher.runpy, "run_module", fake_run_module)

    assert launcher._run_module_in_process("target", ["arg"]) == expected_code
    assert calls == [("target", ["target", "arg"], "__main__", True)]
    assert capsys.readouterr().err == expected_err


class TestMain:
    """main() の引数処理・detach・バージョンガード。"""

    @pytest.fixture(autouse=True)
    def _assume_python_312(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """main の 3.12 ガードを通過させる。古い版のテストは上書きする。"""
        monkeypatch.setattr(launcher.sys, "version_info", (3, 12, 0))

    @staticmethod
    def _stub_detach(monkeypatch: pytest.MonkeyPatch, accepted: bool) -> dict[str, object]:
        """stdin を ``{}`` にし、detach_process を受付結果 ``accepted`` の記録用スタブへ差し替える。"""
        captured: dict[str, object] = {}

        def fake_detach(cmd: list[str], raw: str, *, env: dict[str, str] | None = None) -> bool:
            captured.update(cmd=cmd, raw=raw)
            return accepted

        monkeypatch.setattr("claq.hooks.stdin_io.read_raw_stdin", lambda: "{}")
        monkeypatch.setattr("claq.hooks.detach.detach_process", fake_detach)
        monkeypatch.setattr(launcher, "build_env", lambda: {})
        return captured

    @pytest.mark.parametrize("argv", [[], ["--bg"]], ids=["no-args", "bg-without-target"])
    def test_missing_target_prints_usage(self, capsys: pytest.CaptureFixture[str], argv: list[str]) -> None:
        """ターゲットが無ければ usage を出して 1。"""
        assert launcher.main(argv) == 1
        assert "Usage: python3" in capsys.readouterr().err

    def test_normal_invocation_runs_in_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """通常起動はターゲットと引数をインプロセス実行する。"""
        captured = _stub_run_in_process(monkeypatch)

        assert launcher.main(["claq.hooks.config_protection", "extra"]) == 0
        assert captured == {"target": "claq.hooks.config_protection", "args": ["extra"]}

    @pytest.mark.parametrize(
        ("target", "args"),
        [("claq.hooks.config_protection", ["a", "b"]), ("claq.mem.cli", [])],
        ids=["with-args", "without-args"],
    )
    def test_bg_detaches_module_invocation(self, monkeypatch: pytest.MonkeyPatch, target: str, args: list[str]) -> None:
        """--bg は host に関わらずインプロセス実行せず ``python -m`` で detach し、stdin を渡して 0 を返す。"""
        captured = self._stub_detach(monkeypatch, accepted=True)
        in_process = _stub_run_in_process(monkeypatch)

        assert launcher.main(["--bg", target, *args]) == 0
        assert in_process == {}
        assert captured == {"cmd": [sys.executable, "-m", target, *args], "raw": "{}"}

    def test_bg_detach_failure_returns_nonzero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """detach の受付失敗は非 0 を返し、stderr にエラーを書く。

        docs/adr/hook-failure-direction.md は親の exit code を「子の起動を受け付けたか」と定義する。
        受付に失敗したのに 0 を返すと、呼び出し側は起動されていない処理を受付成功と誤認する。
        """
        self._stub_detach(monkeypatch, accepted=False)

        assert launcher.main(["--bg", "claq.mem.cli", "handoff"]) == 1
        assert "Error detaching" in capsys.readouterr().err

    def test_inserts_src_dir_when_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """src ディレクトリが sys.path に無ければ挿入する。"""
        src = str(launcher.REPO_ROOT / "src")
        monkeypatch.setattr(sys, "path", [p for p in sys.path if p != src])
        _stub_run_in_process(monkeypatch)

        assert launcher.main(["some.target"]) == 0
        assert src in sys.path

    def test_entrypoint_returns_usage_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``python3 -m claq.launcher`` 相当は引数なしで usage 終了する。"""
        monkeypatch.setattr(sys, "argv", ["launcher.py"])

        with pytest.raises(SystemExit) as excinfo:
            runpy.run_module("claq.launcher", run_name="__main__")

        assert excinfo.value.code == 1

    @pytest.mark.parametrize("version", [(3, 9, 6), (3, 11, 9)])
    def test_old_python_returns_zero_without_running(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], version: tuple[int, int, int]
    ) -> None:
        """3.12 未満ではターゲットを実行せず、stdout を汚さず 0 を返す。"""
        monkeypatch.setattr(launcher.sys, "version_info", version)
        in_process = _stub_run_in_process(monkeypatch)

        assert launcher.main(["claq.mem.cli", "context"]) == 0
        assert in_process == {}
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "3.12" in captured.err
        assert ".".join(str(part) for part in version) in captured.err


class TestForceUtf8Streams:
    """`force_utf8_streams`（出力エンコーディングの固定）のテスト。

    stdout がパイプかつ UTF-8 モード無効のとき、Python はロケール由来のエンコーディングを使う
    （Windows の既定コードページ、`LC_ALL=C` の Linux では ASCII）。その状態で日本語を書くと
    UnicodeEncodeError になり、フックは注入も deny もできないまま exit 1 で落ちる。
    """

    @pytest.mark.parametrize("error", [None, OSError("bad fd"), ValueError("detached")], ids=["ok", "oserror", "valueerror"])
    def test_reconfigures_both_streams_to_utf8(self, monkeypatch: pytest.MonkeyPatch, error: Exception | None) -> None:
        """stdout は host が読むデータなので `replace`、stderr は既定の `backslashreplace` で UTF-8 にする。

        stderr を `replace` にすると surrogateescape 由来の不正 UTF-8 ファイル名が ``\\udcXX`` から `?` に落ち、
        診断情報が消える。reconfigure 自体が失敗してもフック本来の処理を止めず、もう一方の設定へ進む。
        """
        calls: list[tuple[str, dict]] = []

        class _Stream:
            """reconfigure の引数を記録し、``error`` があれば送出するストリーム。"""

            def __init__(self, name: str) -> None:
                """記録に使うストリーム名を持つ。"""
                self._name = name

            def reconfigure(self, **kwargs: object) -> None:
                """引数を記録する。"""
                calls.append((self._name, dict(kwargs)))
                if error is not None:
                    raise error

        monkeypatch.setattr(launcher.sys, "stdout", _Stream("stdout"))
        monkeypatch.setattr(launcher.sys, "stderr", _Stream("stderr"))

        launcher.force_utf8_streams()

        assert calls == [
            ("stdout", {"encoding": "utf-8", "errors": "replace"}),
            ("stderr", {"encoding": "utf-8", "errors": "backslashreplace"}),
        ]

    def test_stream_without_reconfigure_is_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`reconfigure` を持たないストリーム（テストの差し替え等）でも落ちないこと。"""
        monkeypatch.setattr(launcher.sys, "stdout", object())
        monkeypatch.setattr(launcher.sys, "stderr", object())

        launcher.force_utf8_streams()


def test_non_ascii_output_survives_a_non_utf8_locale(tmp_path: Path) -> None:
    """UTF-8 でないロケールでも、非 ASCII を含む注入が壊れず exit 0 で返ること（本プロセスの stdout は UTF-8 なので実プロセスで見る）。"""
    from claq.mem.database import Database
    from claq.mem.models import Knowledge, utc_now_iso

    with Database(tmp_path / "mem.db") as db:
        db.upsert_knowledge(
            Knowledge(
                key="k",
                kind="fact",
                scope="global",
                title="検査 ✓ の知識",
                body="b",
                status="active",
                source="human",
                created_at=utc_now_iso(),
                updated_at=utc_now_iso(),
            )
        )

    result = subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), "claq.mem.cli", "context"],
        input=json.dumps({"session_id": "s", "source": "startup"}),
        capture_output=True,
        text=True,
        env=_non_utf8_env(tmp_path),
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "検査 ✓ の知識" in result.stdout


def test_background_child_writes_non_ascii_log_under_non_utf8_locale(tmp_path: Path) -> None:
    """非 UTF-8 ロケールでも、detach 子の日本語ログが壊れず書けること。

    `--bg` 経路は実プロセスでしか再現しない。壊れた handoff payload を渡して
    子に警告を書かせ、bg ログに UnicodeEncodeError が出ないことを見る。
    """
    result = subprocess.run(
        [sys.executable, str(LAUNCHER_PATH), "--bg", "claq.mem.cli", "handoff"],
        input='{"handoff": "日本語の引き継ぎ ✓"}',
        capture_output=True,
        text=True,
        env=_non_utf8_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    # ログが出ないのが正常系なので、短い猶予だけ待って判定する。
    deadline = time.monotonic() + 5
    log_dir = tmp_path / ".claq" / "logs"
    while time.monotonic() < deadline:
        logs = sorted(log_dir.glob("bg-*.log")) if log_dir.is_dir() else []
        if logs and logs[-1].stat().st_size:
            break
        time.sleep(0.1)

    for log in sorted(log_dir.glob("bg-*.log")) if log_dir.is_dir() else []:
        assert "UnicodeEncodeError" not in log.read_text(encoding="utf-8", errors="replace")
