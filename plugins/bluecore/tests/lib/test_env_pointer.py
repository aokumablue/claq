"""bluecore.lib.env_pointer のテスト。

``write_env_pointer`` の Python 側ロジック（roots/ の生成・GC・原子的書き込み・
失敗時の握り潰しと警告）に加え、生成された ``env.sh`` を実際に shell で source し、
PPID/祖先チェーン tier・候補 1 本 fallback・全滅時の 127・悪意あるポインタ内容が
実行されないことを実挙動として検証する（``env-template.sh`` は shell ファイルのため
``--cov`` の対象外であり、テキスト assert だけでは分岐が検証されない）。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from bluecore.lib import env_pointer as mod
from bluecore.lib.constants import BASE_DIR_NAME

_ENV_TEMPLATE = Path(__file__).resolve().parents[2] / "runtime" / "env-template.sh"


def _make_plugin_root(root: Path) -> Path:
    """``runtime/bluecore-helpers.sh`` と ``runtime/env-template.sh`` を持つ最小 plugin root を作る。"""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "bluecore-helpers.sh").write_text(
        'bluecore_run() { printf "ran:%s\\n" "$*"; }\n', encoding="utf-8"
    )
    (runtime_dir / "env-template.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` をテスト用ディレクトリへ差し替える（state dir は ``$HOME`` 固定、R-04）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("BLUECORE_HOME", raising=False)
    return home


class TestWriteEnvPointer:
    """write_env_pointer の Python 側ロジックのテスト。"""

    def test_writes_roots_and_env_sh_as_plain_data(self, tmp_path: Path, _isolate_home: Path) -> None:
        """roots/<ppid> は shell 代入形式ではなく絶対パス 1 行のみであること（R-01/R-03）。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        bluecore_dir = _isolate_home / BASE_DIR_NAME
        roots_dir = bluecore_dir / "roots"
        pid_file = roots_dir / str(os.getppid())
        assert pid_file.read_text(encoding="utf-8") == f"{plugin_root}\n"
        assert "BLUECORE_ROOT" not in pid_file.read_text(encoding="utf-8")
        assert (bluecore_dir / "env.sh").read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")
        assert oct(bluecore_dir.stat().st_mode)[-3:] == "700"
        assert oct(roots_dir.stat().st_mode)[-3:] == "700"

    def test_overwrites_on_repeated_call(self, tmp_path: Path, _isolate_home: Path) -> None:
        """同一 PID から再度呼んでも古い root を上書きする（PID 再利用対策の前提）。"""
        first_root = _make_plugin_root(tmp_path / "first")
        second_root = _make_plugin_root(tmp_path / "second")

        mod.write_env_pointer(first_root)
        mod.write_env_pointer(second_root)

        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        assert pid_file.read_text(encoding="utf-8") == f"{second_root}\n"

    def test_rejects_root_containing_newline(self, tmp_path: Path, _isolate_home: Path) -> None:
        """改行を含む root は書き込まれず、既存の有効ポインタも壊されない。"""
        good_root = _make_plugin_root(tmp_path / "good")
        mod.write_env_pointer(good_root)
        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        before = pid_file.read_text(encoding="utf-8")

        evil_root = tmp_path / "evil\nBLUECORE_INJECTED=1"
        mod.write_env_pointer(evil_root)  # 例外を送出しない（握り潰す）

        assert pid_file.read_text(encoding="utf-8") == before

    def test_swallows_missing_template(self, tmp_path: Path, _isolate_home: Path) -> None:
        """env-template.sh が無くても例外を送出しない（hook を壊さない）。"""
        plugin_root = tmp_path / "plugin-without-template"
        plugin_root.mkdir()

        mod.write_env_pointer(plugin_root)  # 例外を送出しないことを確認

        assert not (_isolate_home / BASE_DIR_NAME / "env.sh").exists()

    def test_swallows_unwritable_home(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """~/.bluecore を作成できない場合も例外を送出しない。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        def _boom(_path: Path) -> Path:
            raise OSError("permission denied")

        monkeypatch.setattr(mod, "_ensure_private_dir", _boom)

        mod.write_env_pointer(plugin_root)  # 例外を送出しないことを確認

    def test_warns_once_per_process_on_failure(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """書き込み失敗は stderr へ 1 回だけ JSON 警告を出す（無音の機能低下を防ぐ）。"""
        monkeypatch.setattr(mod, "_warned_this_process", False)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        def _boom(_path: Path) -> Path:
            raise OSError("permission denied")

        monkeypatch.setattr(mod, "_ensure_private_dir", _boom)

        mod.write_env_pointer(plugin_root)
        mod.write_env_pointer(plugin_root)

        captured = capsys.readouterr()
        lines = [line for line in captured.err.splitlines() if line]
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["bluecoreEnvPointerWriteFailed"] is True

    def test_warn_once_swallows_stderr_write_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stderr への書き込み自体が失敗しても例外を送出しない。"""
        monkeypatch.setattr(mod, "_warned_this_process", False)

        class _BoomStderr:
            def write(self, _text: str) -> int:
                raise OSError("broken pipe")

        monkeypatch.setattr(mod.sys, "stderr", _BoomStderr())

        mod._warn_once("reason")  # 例外を送出しないことを確認

    def test_warn_once_is_noop_on_second_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """2 回目以降の呼び出しでは何も書かない（早期 return の分岐）。"""
        monkeypatch.setattr(mod, "_warned_this_process", True)
        mod._warn_once("reason")  # 何も起きないことを確認（早期 return）

    def test_just_written_pointer_survives_gc_even_if_ppid_looks_dead(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """launcher の親が使い捨て中間 shell だった場合の自壊を防ぐ回帰テスト。

        ``os.getppid()`` が指すプロセスが既に居ない（かつ猶予も切れている）
        状況を強制しても、同じ呼び出しで書いたポインタ自身は GC が消さない
        こと（``_gc_roots`` の ``keep_pid`` 除外が実際に効くこと）を、
        ``write_env_pointer`` 経由の統合シナリオとして確認する。
        """
        monkeypatch.setattr(mod, "_pid_is_alive", lambda _pid: False)
        monkeypatch.setattr(mod, "_DEAD_PID_GRACE_SECONDS", 0)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        assert pid_file.exists()
        assert pid_file.read_text(encoding="utf-8") == f"{plugin_root}\n"


class TestStateHome:
    """_state_home のフォールバック分岐のテスト。"""

    def test_uses_home_env_when_set(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        assert mod._state_home() == tmp_path

    def test_falls_back_to_path_home_when_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
        assert mod._state_home() == tmp_path

    def test_falls_back_to_cwd_when_home_unresolvable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("HOME", raising=False)

        def _boom(cls: object) -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(mod.Path, "home", classmethod(_boom))
        assert mod._state_home() == Path.cwd()

    def test_ignores_bluecore_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """BLUECORE_HOME はテスト隔離専用のノブであり、state dir には影響しない（R-04）。"""
        other = tmp_path / "other"
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("BLUECORE_HOME", str(other))
        assert mod._state_home() == tmp_path


class TestAtomicWrite:
    """_atomic_write_text の原子性・失敗時挙動のテスト。"""

    def test_no_tmp_file_left_behind_on_success(self, tmp_path: Path) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        mod._atomic_write_text(target, "content\n")
        assert target.read_text(encoding="utf-8") == "content\n"
        leftovers = [p for p in workdir.iterdir() if p != target]
        assert leftovers == []

    def test_cleans_up_tmp_file_on_replace_failure(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"

        def _boom_replace(_src: object, _dst: object) -> None:
            raise OSError("replace failed")

        monkeypatch.setattr(mod.os, "replace", _boom_replace)

        with pytest.raises(OSError):
            mod._atomic_write_text(target, "content\n")

        assert not target.exists()
        leftovers = list(workdir.iterdir())
        assert leftovers == []

    def test_tmp_unlink_failure_during_cleanup_is_swallowed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rename 失敗後の tmp ファイル削除自体が失敗しても、元の OSError を送出する。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"

        def _boom_replace(_src: object, _dst: object) -> None:
            raise OSError("replace failed")

        def _boom_unlink(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("unlink failed")

        monkeypatch.setattr(mod.os, "replace", _boom_replace)
        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        with pytest.raises(OSError, match="replace failed"):
            mod._atomic_write_text(target, "content\n")


class TestPidIsAlive:
    """_pid_is_alive の判定分岐のテスト。"""

    def test_self_pid_is_alive(self) -> None:
        assert mod._pid_is_alive(os.getpid()) is True

    def test_zero_pid_is_never_alive(self) -> None:
        """pid=0 は自プロセスグループへのシグナルになるため常に False とする。"""
        assert mod._pid_is_alive(0) is False

    def test_negative_pid_is_never_alive(self) -> None:
        assert mod._pid_is_alive(-1) is False

    def test_dead_pid_is_not_alive(self) -> None:
        proc = subprocess.Popen(["true"])
        proc.wait()
        assert mod._pid_is_alive(proc.pid) is False

    def test_permission_error_counts_as_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_pid: int, _sig: int) -> None:
            raise PermissionError

        monkeypatch.setattr(mod.os, "kill", _boom)
        assert mod._pid_is_alive(12345) is True

    def test_other_oserror_counts_as_not_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(_pid: int, _sig: int) -> None:
            raise OSError("unexpected")

        monkeypatch.setattr(mod.os, "kill", _boom)
        assert mod._pid_is_alive(12345) is False


class TestGcRoots:
    """_gc_roots / _gc_one の削除判定のテスト。"""

    def test_non_digit_names_are_removed(self, tmp_path: Path) -> None:
        """旧形式（<pid>.sh・latest・latest.sh）は無条件で削除する。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        for name in ("999999.sh", "latest", "latest.sh"):
            (roots_dir / name).write_text("/old\n", encoding="utf-8")

        mod._gc_roots(roots_dir)

        assert list(roots_dir.iterdir()) == []

    def test_alive_fresh_pid_is_kept(self, tmp_path: Path) -> None:
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        entry = roots_dir / str(os.getpid())
        entry.write_text("/self\n", encoding="utf-8")

        mod._gc_roots(roots_dir)

        assert entry.exists()

    def test_alive_pid_but_stale_mtime_is_removed(self, tmp_path: Path) -> None:
        """稼働中のホストは毎 hook で mtime を更新するため、古いままなのは PID 再利用と判断する。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        entry = roots_dir / str(os.getpid())
        entry.write_text("/self\n", encoding="utf-8")
        old = time.time() - mod._MAX_ROOT_AGE_SECONDS - 3600
        os.utime(entry, (old, old))

        mod._gc_roots(roots_dir)

        assert not entry.exists()

    def test_dead_pid_within_grace_is_kept(self, tmp_path: Path) -> None:
        """書き込み直後の短命プロセスは 1 サイクル猶予する。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        proc = subprocess.Popen(["true"])
        proc.wait()
        entry = roots_dir / str(proc.pid)
        entry.write_text("/gone\n", encoding="utf-8")

        mod._gc_roots(roots_dir)

        assert entry.exists()

    def test_dead_pid_past_grace_is_removed(self, tmp_path: Path) -> None:
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        proc = subprocess.Popen(["true"])
        proc.wait()
        entry = roots_dir / str(proc.pid)
        entry.write_text("/gone\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(entry, (old, old))

        mod._gc_roots(roots_dir)

        assert not entry.exists()

    def test_keep_pid_survives_even_if_dead_and_past_grace(self, tmp_path: Path) -> None:
        """keep_pid に一致するエントリは、他の削除条件をすべて満たしても残る。

        launcher の親プロセス（``os.getppid()``）が使い捨ての中間 shell を指す
        場合、書いたばかりのポインタが同じ GC 呼び出しの中で「PID 不在かつ
        猶予切れ」に見えて自壊しうる。そうならないことを確認する。
        """
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        proc = subprocess.Popen(["true"])
        proc.wait()
        entry = roots_dir / str(proc.pid)
        entry.write_text("/just-written\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(entry, (old, old))

        mod._gc_roots(roots_dir, keep_pid=proc.pid)

        assert entry.exists()

    def test_gc_stamp_file_is_skipped(self, tmp_path: Path) -> None:
        """GC throttle 用の stamp ファイル自体は roots/ の掃除対象にしない。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        stamp = roots_dir / mod._GC_STAMP_FILENAME
        stamp.write_text("", encoding="utf-8")

        mod._gc_roots(roots_dir)

        assert stamp.exists()

    def test_unlistable_roots_dir_is_noop(self, tmp_path: Path) -> None:
        mod._gc_roots(tmp_path / "does-not-exist")  # 例外を送出しないことを確認

    def test_entry_stat_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        entry = roots_dir / "424242"
        entry.write_text("/x\n", encoding="utf-8")

        real_unlink = Path.unlink

        def _boom_unlink(self: Path, *args: object, **kwargs: object) -> None:
            if self == entry:
                raise OSError("boom")
            return real_unlink(self, *args, **kwargs)

        # 存在しない PID にして削除対象にしつつ、unlink 失敗を注入する
        monkeypatch.setattr(mod, "_pid_is_alive", lambda _pid: False)
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(entry, (old, old))
        monkeypatch.setattr(Path, "unlink", _boom_unlink)

        mod._gc_roots(roots_dir)  # 例外を送出しないことを確認


class TestGcThrottle:
    """_maybe_run_gc の throttle 挙動のテスト。"""

    def test_gc_runs_when_stamp_absent(self, tmp_path: Path) -> None:
        bluecore_dir = tmp_path / "bc"
        bluecore_dir.mkdir()
        roots_dir = bluecore_dir / "roots"
        roots_dir.mkdir()
        (roots_dir / "latest").write_text("/old\n", encoding="utf-8")

        mod._maybe_run_gc(bluecore_dir, roots_dir, keep_pid=None)

        assert not (roots_dir / "latest").exists()
        assert (bluecore_dir / mod._GC_STAMP_FILENAME).exists()

    def test_gc_skipped_when_stamp_is_fresh(self, tmp_path: Path) -> None:
        bluecore_dir = tmp_path / "bc"
        bluecore_dir.mkdir()
        roots_dir = bluecore_dir / "roots"
        roots_dir.mkdir()
        (bluecore_dir / mod._GC_STAMP_FILENAME).touch()
        (roots_dir / "latest").write_text("/old\n", encoding="utf-8")

        mod._maybe_run_gc(bluecore_dir, roots_dir, keep_pid=None)

        assert (roots_dir / "latest").exists()  # throttle により GC は走らない

    def test_gc_runs_when_stamp_is_stale(self, tmp_path: Path) -> None:
        bluecore_dir = tmp_path / "bc"
        bluecore_dir.mkdir()
        roots_dir = bluecore_dir / "roots"
        roots_dir.mkdir()
        stamp = bluecore_dir / mod._GC_STAMP_FILENAME
        stamp.touch()
        old = time.time() - mod._GC_THROTTLE_SECONDS - 60
        os.utime(stamp, (old, old))
        (roots_dir / "latest").write_text("/old\n", encoding="utf-8")

        mod._maybe_run_gc(bluecore_dir, roots_dir, keep_pid=None)

        assert not (roots_dir / "latest").exists()

    def test_stamp_stat_failure_is_treated_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bluecore_dir = tmp_path / "bc"
        bluecore_dir.mkdir()
        roots_dir = bluecore_dir / "roots"
        roots_dir.mkdir()

        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == mod._GC_STAMP_FILENAME:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)

        mod._maybe_run_gc(bluecore_dir, roots_dir, keep_pid=None)  # 例外を送出しないことを確認

    def test_stamp_touch_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        bluecore_dir = tmp_path / "bc"
        bluecore_dir.mkdir()
        roots_dir = bluecore_dir / "roots"
        roots_dir.mkdir()

        def _boom_touch(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "touch", _boom_touch)

        mod._maybe_run_gc(bluecore_dir, roots_dir, keep_pid=None)  # GC 自体は実行される（touch 失敗は無視）


def _run_env_sh(
    home: Path, *, script: str = '. "$HOME/.bluecore/env.sh" || exit 127\nbluecore_run marker\n'
) -> subprocess.CompletedProcess[str]:
    """clean shell で env.sh を実行する。"""
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def _write_pointer(roots_dir: Path, pid: int, root: Path) -> None:
    roots_dir.mkdir(parents=True, exist_ok=True)
    (roots_dir / str(pid)).write_text(f"{root}\n", encoding="utf-8")


def _install_env_sh(home: Path) -> None:
    """``home/.bluecore/env.sh`` にテンプレートを置く（``roots/`` は各テストが用意する）。"""
    (home / BASE_DIR_NAME).mkdir(parents=True, exist_ok=True)
    (home / BASE_DIR_NAME / "env.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")


class TestEnvShRealExecution:
    """生成された env.sh を実際に source し、tier の各分岐を実行検証する。"""

    def test_ppid_pointer_used(self, tmp_path: Path, _isolate_home: Path) -> None:
        """``sh -c`` で起動した子プロセスの $PPID はテストプロセス自身の PID になる。"""
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), pointer_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_ancestor_pointer_used_when_direct_ppid_misses(self, tmp_path: Path, _isolate_home: Path) -> None:
        """直接の $PPID に一致が無くても、祖先チェーンを辿って解決する。"""
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        # このテストプロセス自身（内側 sh -c の祖先）にポインタを置く。直接の
        # $PPID（中間の sh）には置かないことで、祖先チェーン tier を踏ませる。
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), pointer_root)

        script = 'sh -c \'. "$HOME/.bluecore/env.sh" || exit 127; bluecore_run marker\'\n'
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_exactly_one_candidate_fallback_used(self, tmp_path: Path, _isolate_home: Path) -> None:
        """直接 PPID にも祖先にも一致が無くても、roots/ に有効なポインタが 1 本だけなら採用する。"""
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", 987654321, pointer_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_multiple_candidates_agreeing_on_same_root_resolve(self, tmp_path: Path, _isolate_home: Path) -> None:
        """複数の roots/<pid> が同じ root 値なら、祖先不一致でも解決する。

        単一ホストが launcher 起動のたびに異なる（使い捨ての）PID を
        記録し続けるシナリオ（writer 側の PPID が中間 shell を指す場合）の
        救済策。「候補がちょうど 1 本」ではなく「候補が同じ root に合意して
        いる」ことが本質であることを、複数ファイルで確認する。
        """
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        for pid in (111111, 222222, 333333):
            _write_pointer(roots_dir, pid, pointer_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_ambiguous_multiple_candidates_exit_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """祖先不一致かつ候補が複数あるときは、当て推量せず 127 にする（R-05）。"""
        _install_env_sh(_isolate_home)
        root_a = _make_plugin_root(tmp_path / "root-a")
        root_b = _make_plugin_root(tmp_path / "root-b")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        _write_pointer(roots_dir, 111111, root_a)
        _write_pointer(roots_dir, 222222, root_b)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127
        assert "could not resolve" in result.stderr

    def test_stale_conflicting_pointer_is_ignored_by_recency_window(self, tmp_path: Path, _isolate_home: Path) -> None:
        """別ホストの古いポインタは鮮度ウィンドウ外なら候補に数えず、対立させない。

        GC の throttle は 1 時間だが、tier 2 の合意判定はそれとは独立に
        直近 5 分だけを見る。使われなくなったホストのポインタが GC される
        までの最大 1 時間、単独稼働中の別ホストを道連れに 127 へ落とす
        ことを防ぐ（advisor 指摘の cross-host false-conflict window）。
        """
        _install_env_sh(_isolate_home)
        stale_root = _make_plugin_root(tmp_path / "stale-other-host-root")
        fresh_root = _make_plugin_root(tmp_path / "fresh-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        stale_entry = roots_dir / "555555"
        _write_pointer(roots_dir, 555555, stale_root)
        old = time.time() - 10 * 60
        os.utime(stale_entry, (old, old))
        _write_pointer(roots_dir, 666666, fresh_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_single_idle_candidate_still_resolves_regardless_of_age(
        self, tmp_path: Path, _isolate_home: Path
    ) -> None:
        """対立が無ければ、鮮度ウィンドウ外の唯一の候補でも解決する。

        鮮度フィルタは「対立を解消するため」だけに使う（advisor 指摘）。
        候補が 1 つしか無く誰とも対立していないなら、それがどれだけ古くても
        単独稼働ホストの正当な記録であり、単に長時間アイドルだっただけで
        127 にしてはならない。
        """
        _install_env_sh(_isolate_home)
        idle_root = _make_plugin_root(tmp_path / "idle-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        idle_entry = roots_dir / "555555"
        _write_pointer(roots_dir, 555555, idle_root)
        old = time.time() - 10 * 60
        os.utime(idle_entry, (old, old))

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_exits_127_when_no_candidates(self, _isolate_home: Path) -> None:
        _install_env_sh(_isolate_home)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127
        assert "could not resolve the plugin root" in result.stderr

    def test_malicious_pointer_content_is_not_executed(self, _isolate_home: Path) -> None:
        """R-01: ポインタに shell コードを書いても実行されない。"""
        _install_env_sh(_isolate_home)
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir()
        (roots_dir / str(os.getpid())).write_text(
            "printf ROOT_FILE_EXECUTED >&2\nBLUECORE_ROOT=/x\n", encoding="utf-8"
        )

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
        # 複数行なので 1 行目 "printf ROOT_FILE_EXECUTED >&2" が root として扱われ、
        # helpers.sh が存在しないパスのため解決失敗 → 127 になる。
        assert result.returncode == 127

    def test_root_with_special_characters_resolves(self, tmp_path: Path, _isolate_home: Path) -> None:
        """R-03: $・空白・二重引用符・backtick を含む root がシェル展開されず解決できる。"""
        _install_env_sh(_isolate_home)
        odd_root = _make_plugin_root(tmp_path / 'plugin$var with space and "quote`tick')
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), odd_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_symlink_pointer_is_rejected(self, tmp_path: Path, _isolate_home: Path) -> None:
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        real_pointer = tmp_path / "real-pointer"
        real_pointer.write_text(f"{pointer_root}\n", encoding="utf-8")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir()
        (roots_dir / str(os.getpid())).symlink_to(real_pointer)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127

    def test_empty_pointer_file_is_ignored(self, _isolate_home: Path) -> None:
        _install_env_sh(_isolate_home)
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir()
        (roots_dir / str(os.getpid())).write_text("", encoding="utf-8")

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127

    def test_end_to_end_via_write_env_pointer(self, tmp_path: Path, _isolate_home: Path) -> None:
        """write_env_pointer が書いた env.sh をそのまま source して解決できる（統合確認）。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_gc_removed_dead_pointer_then_resolver_exits_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """GC が不在 PID のポインタを消した後、resolver がそれを 127 として扱うこと。

        個々には ``TestGcRoots``（削除判定）と ``TestEnvShRealExecution``
        （resolver の 127 分岐）で検証済みだが、両者を跨ぐ合成的な振る舞い
        （GC が実際に消し、その結果を resolver が正しく「候補ゼロ」として
        扱う）をここで固定する。
        """
        _install_env_sh(_isolate_home)
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        proc = subprocess.Popen(["true"])
        proc.wait()
        dead_entry = roots_dir / str(proc.pid)
        dead_entry.write_text(f"{tmp_path / 'stale-root'}\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(dead_entry, (old, old))

        mod._gc_roots(roots_dir)
        assert not dead_entry.exists()

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127
        assert "could not resolve" in result.stderr

    def test_pointer_with_embedded_nul_does_not_execute_anything(self, _isolate_home: Path) -> None:
        """writer は NUL を書かないが、resolver 側も多層防御として NUL 入りポインタで
        コードを実行しないことを確認する（読み取り結果がどう切れても shell として
        評価されないことが本質。R-01 の防御は format 変更そのものにあるため、
        結果が 0/127 のどちらでも良く、実行痕跡が無いことだけを検証する）。
        """
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        (roots_dir / str(os.getpid())).write_bytes(b"printf ROOT_FILE_EXECUTED >&2\x00/tmp/x\n")
        _install_env_sh(_isolate_home)

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
