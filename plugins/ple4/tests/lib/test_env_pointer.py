"""ple4.lib.env_pointer のテスト。

``write_env_pointer`` の Python 側ロジック（祖先チェーンの解決・GC・
原子的書き込み・失敗時の握り潰しと警告）に加え、生成された ``env.sh`` を
実際に shell で source し、祖先 walk・``lstart`` 一致判定・PID 再利用の拒否・
全滅時の 127・悪意あるポインタ内容が実行されないことを実挙動として検証する
（``env-template.sh`` は shell ファイルのため ``--cov`` の対象外であり、
テキスト assert だけでは分岐が検証されない）。
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path

import pytest

from ple4.lib import env_pointer as mod
from ple4.lib.constants import BASE_DIR_NAME

_ENV_TEMPLATE = Path(__file__).resolve().parents[2] / "runtime" / "env-template.sh"


def _make_plugin_root(root: Path) -> Path:
    """``runtime/ple4-helpers.sh`` と ``runtime/env-template.sh`` を持つ最小 plugin root を作る。"""
    runtime_dir = root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "ple4-helpers.sh").write_text(
        'ple4_run() { printf "ran:%s\\n" "$*"; }\n', encoding="utf-8"
    )
    (runtime_dir / "env-template.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """``HOME`` をテスト用ディレクトリへ差し替える（state dir は ``$HOME`` 固定、R-04）。"""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("PLE4_HOME", raising=False)
    return home


class TestWriteEnvPointer:
    """write_env_pointer の Python 側ロジックのテスト。"""

    def test_writes_roots_and_env_sh_as_plain_data(self, tmp_path: Path, _isolate_home: Path) -> None:
        """roots/<ppid> は shell 代入形式ではなく root + lstart の 2 行のみであること
        （R-01/R-03）。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        ple4_dir = _isolate_home / BASE_DIR_NAME
        roots_dir = ple4_dir / "roots"
        pid_file = roots_dir / str(os.getppid())
        lines = pid_file.read_text(encoding="utf-8").split("\n")
        assert lines[0] == str(plugin_root)
        assert lines[1] != ""  # lstart が記録されている
        assert "PLE4_ROOT" not in pid_file.read_text(encoding="utf-8")
        assert (ple4_dir / "env.sh").read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")
        assert oct(ple4_dir.stat().st_mode)[-3:] == "700"
        assert oct(roots_dir.stat().st_mode)[-3:] == "700"

    def test_skips_ancestor_scan_where_pointers_cannot_resolve(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """祖先ポインタ方式が成立しない OS では `ps` を呼ばず、env.sh だけ書くこと。

        Windows には `ps` が無く、呼んでも空チェーンになるだけだが、全 hook 起動
        ごとの無駄な spawn になる。さらに MSYS/Cygwin 由来の `ps.exe` が PATH に
        あると別 PID 空間の値を拾い、無意味なポインタを書きうる。
        """
        called: list[int] = []
        monkeypatch.setattr(mod, "ancestor_pointers_supported", lambda: False)
        monkeypatch.setattr(mod, "_resolve_ancestor_chain", lambda depth: called.append(depth) or [])
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        ple4_dir = _isolate_home / BASE_DIR_NAME
        assert called == []
        assert [p.name for p in (ple4_dir / "roots").iterdir() if p.name.isdigit()] == []
        assert (ple4_dir / "env.sh").is_file()

    def test_writes_ancestor_chain_not_just_direct_ppid(self, tmp_path: Path, _isolate_home: Path) -> None:
        """H-02: 直接 PPID だけでなく祖先（最大 ``_MAX_ANCESTOR_DEPTH`` 段）にも書く。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        written = {p.name for p in roots_dir.iterdir() if p.name.isdigit()}
        assert str(os.getppid()) in written
        # 祖先チェーンの実測段数は環境依存（pytest プロセスの親が既に
        # init/1 の場合は 1 段で打ち切られる）だが、_MAX_ANCESTOR_DEPTH を
        # 超えて書くことは無い。
        assert 1 <= len(written) <= mod._MAX_ANCESTOR_DEPTH

    def test_overwrites_on_repeated_call(self, tmp_path: Path, _isolate_home: Path) -> None:
        """同一 root で再度呼んでも内容は変わらない（PID 再利用時に自己修復する前提）。"""
        first_root = _make_plugin_root(tmp_path / "first")

        mod.write_env_pointer(first_root)
        mod.write_env_pointer(first_root)

        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        assert pid_file.read_text(encoding="utf-8").split("\n")[0] == str(first_root)

    def test_conflicting_root_overwrites_the_pointer(self, tmp_path: Path, _isolate_home: Path) -> None:
        """異なる root で再度呼ぶと単純に上書きされる（poison はしない）。

        writer が祖先 PID へ書き込む範囲は host インスタンス専有の PID
        （最大 2 段）に限られ、他 host と共有されない。同一 PID に別の
        root が観測されるのは「同一 host がプラグインをアップグレード
        した」ケースのみなので、上書きが正しい（旧 poison 設計は
        advisor レビューで「共有されうる祖先が生存し続ける限り恒久化する
        自己修復不能な失敗モード」と指摘され撤回した）。
        """
        first_root = _make_plugin_root(tmp_path / "first")
        second_root = _make_plugin_root(tmp_path / "second")

        mod.write_env_pointer(first_root)
        mod.write_env_pointer(second_root)

        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        assert pid_file.read_text(encoding="utf-8").split("\n")[0] == str(second_root)

    def test_rejects_root_containing_newline(self, tmp_path: Path, _isolate_home: Path) -> None:
        """改行を含む root は書き込まれず、既存の有効ポインタも壊されない。"""
        good_root = _make_plugin_root(tmp_path / "good")
        mod.write_env_pointer(good_root)
        pid_file = _isolate_home / BASE_DIR_NAME / "roots" / str(os.getppid())
        before = pid_file.read_text(encoding="utf-8")

        evil_root = tmp_path / "evil\nPLE4_INJECTED=1"
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
        """~/.ple4 を作成できない場合も例外を送出しない。"""
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
        assert payload["ple4EnvPointerWriteFailed"] is True

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

    def test_just_written_pointers_survive_gc_even_if_pids_look_dead(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """祖先チェーン全体が使い捨てプロセスだった場合の自壊を防ぐ回帰テスト。

        祖先チェーンの PID が全て既に居ない（かつ猶予も切れている）状況を
        強制しても、同じ呼び出しで書いたポインタ群は GC が消さないこと
        （``_gc_roots`` の ``keep_pids`` 除外が実際に効くこと）を、
        ``write_env_pointer`` 経由の統合シナリオとして確認する。
        """
        monkeypatch.setattr(mod, "_pid_is_alive", lambda _pid: False)
        monkeypatch.setattr(mod, "_DEAD_PID_GRACE_SECONDS", 0)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        pid_file = roots_dir / str(os.getppid())
        assert pid_file.exists()
        assert pid_file.read_text(encoding="utf-8").split("\n")[0] == str(plugin_root)


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

    def test_ignores_ple4_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """PLE4_HOME はテスト隔離専用のノブであり、state dir には影響しない（R-04）。"""
        other = tmp_path / "other"
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("PLE4_HOME", str(other))
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

    def test_fdopen_failure_closes_fd_and_removes_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fdopen が失敗したら mkstemp の fd を閉じ、tmp を残さない。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        closed: list[int] = []
        real_close = os.close

        def _boom_fdopen(_fd: int, *_args: object, **_kwargs: object) -> object:
            raise OSError("fdopen failed")

        def _track_close(fd: int) -> None:
            closed.append(fd)
            real_close(fd)

        monkeypatch.setattr(mod.os, "fdopen", _boom_fdopen)
        monkeypatch.setattr(mod.os, "close", _track_close)

        with pytest.raises(OSError, match="fdopen failed"):
            mod._atomic_write_text(target, "content\n")

        assert not target.exists()
        assert list(workdir.iterdir()) == []
        assert closed

    def test_fdopen_failure_swallows_close_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """fdopen 失敗後の close 自体が失敗しても、元の OSError を送出する。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"

        def _boom_fdopen(_fd: int, *_args: object, **_kwargs: object) -> object:
            raise OSError("fdopen failed")

        real_close = os.close

        def _boom_close(fd: int) -> None:
            real_close(fd)
            raise OSError("close failed")

        monkeypatch.setattr(mod.os, "fdopen", _boom_fdopen)
        monkeypatch.setattr(mod.os, "close", _boom_close)

        with pytest.raises(OSError, match="fdopen failed"):
            mod._atomic_write_text(target, "content\n")

        assert not target.exists()
        assert list(workdir.iterdir()) == []

    def test_tmp_name_uses_random_suffix_not_pid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """一時ファイル名は ``<name>.tmp.<random>`` であり PID 固定ではない。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        seen: list[str] = []
        real_replace = os.replace

        def _capture(src: object, dst: object) -> None:
            seen.append(Path(str(src)).name)
            real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _capture)
        mod._atomic_write_text(target, "content\n")

        assert seen
        name = seen[0]
        prefix = f"{target.name}.tmp."
        assert name.startswith(prefix)
        suffix = name[len(prefix) :]
        assert suffix
        assert suffix != str(os.getpid())
        assert not name.isdigit()

    def test_precreated_pid_named_tmp_symlink_is_not_followed(self, tmp_path: Path) -> None:
        """予測可能な ``<name>.tmp.<pid>`` symlink を追従して victim を壊さない。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        victim = workdir / "victim.txt"
        victim.write_text("untouched\n", encoding="utf-8")
        bait = workdir / f"{target.name}.tmp.{os.getpid()}"
        bait.symlink_to(victim)

        mod._atomic_write_text(target, "new\n")

        assert target.read_text(encoding="utf-8") == "new\n"
        assert victim.read_text(encoding="utf-8") == "untouched\n"
        assert bait.is_symlink()

    def test_stale_pid_named_tmp_does_not_block_next_write(self, tmp_path: Path) -> None:
        """旧 PID 名の stale tmp が残っていても次の write は別ファイルで成功する。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        stale = workdir / f"{target.name}.tmp.{os.getpid()}"
        stale.write_text("stale\n", encoding="utf-8")

        mod._atomic_write_text(target, "fresh\n")

        assert target.read_text(encoding="utf-8") == "fresh\n"
        assert stale.read_text(encoding="utf-8") == "stale\n"

    def test_same_process_concurrent_writes_leave_complete_file(self, tmp_path: Path) -> None:
        """barrier 同期した複数スレッドが同じ target を書いても完全なレコードだけが残る。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        target = workdir / "out.txt"
        payloads = [f"payload-{i:03d}\n" for i in range(8)]
        barrier = threading.Barrier(len(payloads), timeout=5)
        errors: list[BaseException] = []

        def _write(text: str) -> None:
            try:
                barrier.wait()
                mod._atomic_write_text(target, text)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=_write, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
        assert errors == []
        assert target.read_text(encoding="utf-8") in payloads
        leftovers = [path for path in workdir.iterdir() if path != target]
        assert leftovers == []

    def test_write_env_pointer_swallows_mkstemp_oserror(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """mkstemp が OSError でも write_env_pointer は hook を壊さない。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        def _boom(*_args: object, **_kwargs: object) -> tuple[int, str]:
            raise OSError("mkstemp failed")

        monkeypatch.setattr(mod.tempfile, "mkstemp", _boom)
        mod.write_env_pointer(plugin_root)


class TestResolveAncestorChain:
    """_resolve_ancestor_chain の祖先探索ロジックのテスト。"""

    def test_real_chain_includes_own_ppid(self) -> None:
        """モックなしの実行で、チェーンの先頭が os.getppid() と一致すること。"""
        chain = mod._resolve_ancestor_chain(mod._MAX_ANCESTOR_DEPTH)
        assert chain
        assert chain[0][0] == os.getppid()
        assert chain[0][1] != ""

    def test_empty_when_ps_binary_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise FileNotFoundError("ps not found")

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        assert mod._resolve_ancestor_chain(4) == []

    def test_empty_when_ps_times_out(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(*_args: object, **_kwargs: object) -> None:
            raise mod.subprocess.TimeoutExpired(cmd="ps", timeout=5)

        monkeypatch.setattr(mod.subprocess, "run", _boom)
        assert mod._resolve_ancestor_chain(4) == []

    def test_empty_when_ps_returns_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Result:
            returncode = 1
            stdout = ""

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())
        assert mod._resolve_ancestor_chain(4) == []

    def test_chain_walks_synthetic_process_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """合成したプロセス表から、祖先チェーンが正しく計算されること。"""
        own_ppid = os.getppid()
        table = (
            f"{own_ppid} 5000 Thu Aug 20 10:00:00 2026\n"
            "5000 6000 Thu Aug 20 09:00:00 2026\n"
            "6000 7000 Thu Aug 20 08:00:00 2026\n"
            "7000 1 Thu Aug 20 07:00:00 2026\n"
            "1 0 Thu Aug 20 00:00:00 2026\n"
        )

        class _Result:
            returncode = 0
            stdout = table

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        chain = mod._resolve_ancestor_chain(4)

        assert [pid for pid, _lstart in chain] == [own_ppid, 5000, 6000, 7000]
        assert chain[0][1] == "Thu Aug 20 10:00:00 2026"

    def test_chain_stops_at_max_depth(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own_ppid = os.getppid()
        table = (
            f"{own_ppid} 5000 t0\n"
            "5000 6000 t1\n"
            "6000 7000 t2\n"
            "7000 8000 t3\n"
            "8000 9000 t4\n"
        )

        class _Result:
            returncode = 0
            stdout = table

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        chain = mod._resolve_ancestor_chain(2)

        assert [pid for pid, _lstart in chain] == [own_ppid, 5000]

    def test_chain_stops_when_own_ppid_missing_from_table(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _Result:
            returncode = 0
            stdout = "999999999 1 t0\n"  # os.getppid() を含まない

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        assert mod._resolve_ancestor_chain(4) == []

    def test_malformed_lines_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        own_ppid = os.getppid()
        table = (
            "not-a-pid ppid lstart\n"  # int 変換失敗
            "123\n"  # フィールド不足
            f"{own_ppid} notanint t0\n"  # ppid が int に変換できない
        )

        class _Result:
            returncode = 0
            stdout = table

        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _Result())

        # own_ppid 自体の行が壊れているため、チェーンは組み立てられない
        assert mod._resolve_ancestor_chain(4) == []


class TestWriteAncestorPointer:
    """_write_ancestor_pointer の分岐のテスト。"""

    def test_writes_new_pointer(self, tmp_path: Path) -> None:
        path = tmp_path / "42"
        mod._write_ancestor_pointer(path, "/root/a", "lstart-a")
        assert path.read_text(encoding="utf-8") == "/root/a\nlstart-a\n"

    def test_refreshes_matching_root(self, tmp_path: Path) -> None:
        path = tmp_path / "42"
        mod._write_ancestor_pointer(path, "/root/a", "lstart-a")
        mod._write_ancestor_pointer(path, "/root/a", "lstart-a-updated")
        assert path.read_text(encoding="utf-8") == "/root/a\nlstart-a-updated\n"

    def test_conflicting_root_overwrites(self, tmp_path: Path) -> None:
        """異なる root で再度呼ぶと単純に上書きされる（poison はしない、書き込み
        対象 PID は host インスタンス専有のため衝突自体が起こらない前提）。"""
        path = tmp_path / "42"
        mod._write_ancestor_pointer(path, "/root/a", "lstart-a")
        mod._write_ancestor_pointer(path, "/root/b", "lstart-b")
        assert path.read_text(encoding="utf-8") == "/root/b\nlstart-b\n"

    def test_write_failure_propagates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        path = tmp_path / "42"

        def _boom(_path: Path, _text: str) -> None:
            raise OSError("disk full")

        monkeypatch.setattr(mod, "_atomic_write_text", _boom)

        with pytest.raises(OSError, match="disk full"):
            mod._write_ancestor_pointer(path, "/root/a", "lstart-a")


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

    def test_non_digit_names_past_grace_are_removed(self, tmp_path: Path) -> None:
        """旧形式（<pid>.sh・latest・latest.sh）は猶予後に削除する。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        for name in ("999999.sh", "latest", "latest.sh"):
            entry = roots_dir / name
            entry.write_text("/old\n", encoding="utf-8")
            os.utime(entry, (old, old))

        mod._gc_roots(roots_dir)

        assert list(roots_dir.iterdir()) == []

    def test_fresh_non_digit_temp_file_survives_gc(self, tmp_path: Path) -> None:
        """M-01: 別 writer が rename 直前に作った一時ファイルを race で消さない。

        ``roots/`` で実際に発生しうるのは ``<name>.tmp.<random>``
        （mkstemp）。旧 writer 残骸（``<pid>.tmp.<writer_pid>``）と、
        レポートが ``roots/`` に置いて再現していた ``env.sh.tmp.<pid>`` も
        非数字名として同じ age-gate 対象になる。
        """
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        for name in ("env.sh.tmp.12345", "555555.tmp.666666", "555555.tmp.abcdefgh"):
            (roots_dir / name).write_text("x\n", encoding="utf-8")

        mod._gc_roots(roots_dir)

        assert (roots_dir / "env.sh.tmp.12345").exists()
        assert (roots_dir / "555555.tmp.666666").exists()
        assert (roots_dir / "555555.tmp.abcdefgh").exists()

    def test_fresh_env_sh_tmp_in_ple4_dir_survives_gc(self, tmp_path: Path) -> None:
        """ple4_dir 直下の新鮮な env.sh.tmp.* は age-gate で残る。"""
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        fresh = ple4_dir / f"{mod._ENV_TMP_PREFIX}freshxxx"
        fresh.write_text("in-flight\n", encoding="utf-8")

        mod._gc_stale_temp_files(ple4_dir, time.time())

        assert fresh.exists()

    def test_stale_env_sh_tmp_in_ple4_dir_is_removed(self, tmp_path: Path) -> None:
        """猶予を過ぎた env.sh.tmp.* は ple4_dir から回収する。"""
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        stale = ple4_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(stale, (old, old))

        mod._gc_stale_temp_files(ple4_dir, time.time())

        assert not stale.exists()

    def test_gc_env_sh_tmp_does_not_touch_env_sh_or_mem_db(self, tmp_path: Path) -> None:
        """プレフィックス不一致の state ファイルは env.sh tmp GC の対象外。

        survivor も stale tmp と同じ古い mtime にする。新鮮なまま残すと、
        プレフィックスを ``env.sh`` まで広げた実装でもこのテストは緑になる。
        """
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        env_sh = ple4_dir / mod._ENV_FILENAME
        mem_db = ple4_dir / "mem.db"
        other = ple4_dir / "unrelated.txt"
        env_sh.write_text("keep-env\n", encoding="utf-8")
        mem_db.write_text("keep-db\n", encoding="utf-8")
        other.write_text("keep-other\n", encoding="utf-8")
        stale = ple4_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        for path in (env_sh, mem_db, other, stale):
            os.utime(path, (old, old))

        mod._gc_stale_temp_files(ple4_dir, time.time())

        assert not stale.exists()
        assert env_sh.read_text(encoding="utf-8") == "keep-env\n"
        assert mem_db.read_text(encoding="utf-8") == "keep-db\n"
        assert other.read_text(encoding="utf-8") == "keep-other\n"

    def test_unlistable_ple4_dir_is_noop_for_env_sh_tmp_gc(self, tmp_path: Path) -> None:
        """ple4_dir が列挙できなくても env.sh tmp GC は例外を送出しない。"""
        mod._gc_stale_temp_files(tmp_path / "does-not-exist", time.time())

    def test_env_sh_tmp_stat_failure_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env.sh.tmp.* の stat 失敗は無視する。"""
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        entry = ple4_dir / f"{mod._ENV_TMP_PREFIX}statfail"
        entry.write_text("x\n", encoding="utf-8")
        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)
        mod._gc_stale_temp_files(ple4_dir, time.time())
        assert entry.exists()

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

    def test_keep_pids_survives_even_if_dead_and_past_grace(self, tmp_path: Path) -> None:
        """keep_pids に含まれるエントリは、他の削除条件をすべて満たしても残る。

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

        mod._gc_roots(roots_dir, keep_pids=frozenset({proc.pid}))

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
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        roots_dir = ple4_dir / "roots"
        roots_dir.mkdir()
        legacy = roots_dir / "latest"
        legacy.write_text("/old\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(legacy, (old, old))
        stale_env_tmp = ple4_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale_env_tmp.write_text("orphan\n", encoding="utf-8")
        os.utime(stale_env_tmp, (old, old))

        mod._maybe_run_gc(ple4_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()
        assert not stale_env_tmp.exists()
        assert (ple4_dir / mod._GC_STAMP_FILENAME).exists()

    def test_gc_skipped_when_stamp_is_fresh(self, tmp_path: Path) -> None:
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        roots_dir = ple4_dir / "roots"
        roots_dir.mkdir()
        (ple4_dir / mod._GC_STAMP_FILENAME).touch()
        (roots_dir / "latest").write_text("/old\n", encoding="utf-8")
        stale_env_tmp = ple4_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale_env_tmp.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(stale_env_tmp, (old, old))

        mod._maybe_run_gc(ple4_dir, roots_dir, keep_pids=frozenset())

        assert (roots_dir / "latest").exists()  # throttle により GC は走らない
        assert stale_env_tmp.exists()

    def test_gc_runs_when_stamp_is_stale(self, tmp_path: Path) -> None:
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        roots_dir = ple4_dir / "roots"
        roots_dir.mkdir()
        stamp = ple4_dir / mod._GC_STAMP_FILENAME
        stamp.touch()
        stamp_old = time.time() - mod._GC_THROTTLE_SECONDS - 60
        os.utime(stamp, (stamp_old, stamp_old))
        legacy = roots_dir / "latest"
        legacy.write_text("/old\n", encoding="utf-8")
        legacy_old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(legacy, (legacy_old, legacy_old))

        mod._maybe_run_gc(ple4_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()

    def test_stamp_stat_failure_is_treated_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        roots_dir = ple4_dir / "roots"
        roots_dir.mkdir()

        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == mod._GC_STAMP_FILENAME:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)

        mod._maybe_run_gc(ple4_dir, roots_dir, keep_pids=frozenset())  # 例外を送出しないことを確認

    def test_stamp_touch_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        ple4_dir = tmp_path / "bc"
        ple4_dir.mkdir()
        roots_dir = ple4_dir / "roots"
        roots_dir.mkdir()

        def _boom_touch(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "touch", _boom_touch)

        mod._maybe_run_gc(ple4_dir, roots_dir, keep_pids=frozenset())  # GC 自体は実行される（touch 失敗は無視）


def _run_env_sh(
    home: Path, *, script: str = '. "$HOME/.ple4/env.sh" || exit 127\nple4_run marker\n'
) -> subprocess.CompletedProcess[str]:
    """clean shell で env.sh を実行する。"""
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def _lstart_for(pid: int) -> str:
    """``pid`` の現在の起動時刻を ``LC_ALL=C`` で取得する（テスト用ヘルパー）。

    writer 実装（``_resolve_ancestor_chain``）と同じロケール固定・
    非正規化ポリシーで取得する。存在しない PID には空文字列を返す。
    """
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    )
    return result.stdout.rstrip("\n")


def _write_pointer(roots_dir: Path, pid: int, root: Path, *, lstart: str | None = None) -> None:
    """``roots/<pid>`` へ ``root\\nlstart\\n`` の 2 行フォーマットで書く。

    ``lstart`` を省略した場合、``pid`` 自身の現在の起動時刻を使う（テストが
    実在する PID — 自プロセスや生成した子プロセス — を使う場合の便宜）。
    存在しない PID を意図的に使う、または PID 再利用を装いたい場合は
    明示的な文字列を渡す。
    """
    roots_dir.mkdir(parents=True, exist_ok=True)
    if lstart is None:
        lstart = _lstart_for(pid)
    (roots_dir / str(pid)).write_text(f"{root}\n{lstart}\n", encoding="utf-8")


def _install_env_sh(home: Path) -> None:
    """``home/.ple4/env.sh`` にテンプレートを置く（``roots/`` は各テストが用意する）。"""
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

        script = 'sh -c \'. "$HOME/.ple4/env.sh" || exit 127; ple4_run marker\'\n'
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_ancestor_beyond_depth_limit_exits_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """3 段祖先（walk 上限 2 段の外側）にしかポインタが無ければ、推測せず 127 にする。

        writer は host インスタンス専有の 2 段（bash tool の shell とその親）
        にしか書かない設計（advisor レビュー、poison 廃止の裏返し）。resolver
        の walk 上限も同じ 2 段であることをここで固定する。深いネストの文字列
        エスケープを避けるため、各層を argv リストとして組み立て、
        ``shlex.quote`` で 1 段ずつ包む。
        """
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        # テストプロセス自身（inner から見て depth 2）にのみポインタを置く —
        # walk 上限（depth 0/1）の外側なので解決できないはず。
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), pointer_root)

        script_file = tmp_path / "depth3.sh"
        script_file.write_text('. "$HOME/.ple4/env.sh" || exit 127\nple4_run marker\n', encoding="utf-8")

        # `; exit $?` を各層に付ける: 単一コマンドだけの `sh -c` はシェルが
        # tail-call 最適化で自分自身を exec 置換することがあり（実機で
        # 確認済み — `sh -c "sh -c 'ps ...'"` の ppid チェーンが 1 段
        # 潰れる）、意図した 3 段ネストが実際には 1〜2 段にしかならない。
        # 複数コマンドにすることで exec 置換を防ぎ、fork による本物の
        # 3 段プロセスチェーンを作る。終了コードは `$?` で伝播する。
        inner = ["sh", str(script_file)]
        middle_script = " ".join(shlex.quote(x) for x in inner) + "; exit $?"
        middle = ["sh", "-c", middle_script]
        outer_script = " ".join(shlex.quote(x) for x in middle) + "; exit $?"
        outer = ["sh", "-c", outer_script]
        result = subprocess.run(
            outer, capture_output=True, text=True, env={"HOME": str(_isolate_home), "PATH": "/usr/bin:/bin"}
        )

        assert result.returncode == 127
        assert "could not resolve" in result.stderr

    def test_pid_reuse_at_direct_ppid_falls_through_to_ancestor(self, tmp_path: Path, _isolate_home: Path) -> None:
        """直接 PPID の記録された lstart が現在値と食い違えば（PID 再利用）
        スキップし、さらに祖先を辿って解決する。"""
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        # 祖先（このテストプロセス自身）には正しいポインタを置く。
        _write_pointer(roots_dir, os.getpid(), pointer_root)

        # 内側 sh -c の直接 PPID（中間 sh）には、実行時に自分自身の $$ を
        # 使って「実際の lstart とは一致しない」ポインタを書き込ませる。
        script = (
            'printf "not-a-real-root\\nintentionally-wrong-lstart\\n" > "$HOME/.ple4/roots/$$"\n'
            'sh -c \'. "$HOME/.ple4/env.sh" || exit 127; ple4_run marker\'\n'
        )
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_malformed_ancestor_is_skipped_and_walk_continues(self, tmp_path: Path, _isolate_home: Path) -> None:
        """空/不正なポインタはスキップし、さらに遠い祖先で解決する。

        writer はもはや衝突検知で意図的に空ファイルを作らない（poison は
        advisor レビューで撤回済み）が、resolver 側は防御として空・欠落
        ポインタを不正データ扱いでスキップし続ける（read 側の余剰防御。
        `[ -z ... ]` チェックは変更していない）。"""
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        # 祖先（このテストプロセス自身）に正しいポインタを置く。
        _write_pointer(roots_dir, os.getpid(), pointer_root)

        # 直接 PPID（中間 sh）のポインタは空にしておく。
        script = (
            'printf "" > "$HOME/.ple4/roots/$$"\n'
            'sh -c \'. "$HOME/.ple4/env.sh" || exit 127; ple4_run marker\'\n'
        )
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_h02_two_unrelated_alive_hosts_exit_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """H-02 のレポート再現: 祖先ではない 2 つの実在プロセスの root が
        対立していても、当て推量せず 127 にする（旧設計は root 値合意の
        recent fallback で片方を静かに採用していた）。
        """
        _install_env_sh(_isolate_home)
        root_a = _make_plugin_root(tmp_path / "root-a")
        root_b = _make_plugin_root(tmp_path / "root-b")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"

        proc_a = subprocess.Popen(["sleep", "5"])
        proc_b = subprocess.Popen(["sleep", "5"])
        try:
            _write_pointer(roots_dir, proc_a.pid, root_a)
            _write_pointer(roots_dir, proc_b.pid, root_b)

            result = _run_env_sh(_isolate_home)

            assert result.returncode == 127
            assert "could not resolve" in result.stderr
        finally:
            proc_a.terminate()
            proc_b.terminate()
            proc_a.wait()
            proc_b.wait()

    def test_resolves_when_writer_runs_under_non_c_locale(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """writer（env_pointer.py）の ambient locale が C でなくても、resolver
        （``env -i`` 相当の最小環境、事実上 C ロケール）から解決できること。

        ``ps -o lstart=`` の出力はロケール依存（``LANG=ja_JP.UTF-8`` では
        ``木  8/20 ...``、``C`` ロケールでは ``Thu Aug 20 ...``）であることを
        実機で確認した。writer/resolver 双方が ``LC_ALL=C`` を明示するため、
        ambient locale の違いに関わらず一致する必要がある — これは実機で
        踏んだ回帰（writer 側の ``LC_ALL`` 指定漏れ）を固定する。

        ``write_env_pointer`` は内部で常に ``LC_ALL=C`` を注入するため、単に
        ``LANG`` を設定して解決成功を確認するだけでは、``LC_ALL`` 注入を
        誤って外した future 回帰を検知できない（advisor レビュー指摘）。
        先に「``LC_ALL=C`` 版と ``LANG=ja_JP.UTF-8``（``LC_ALL`` 無し）版の
        ``ps -o lstart=`` が実際に異なる」ことを確認し、ロケール差分が
        存在する環境でだけ本題の解決成功を assert する（差分が無い環境
        ―― 日本語ロケール未インストール等 ―― では xfail 相当としてスキップし、
        偽の green を防ぐ）。
        """
        own_pid = os.getpid()
        c_lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(own_pid)],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        ).stdout
        ja_env = {**os.environ, "LANG": "ja_JP.UTF-8"}
        ja_env.pop("LC_ALL", None)
        ja_lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(own_pid)], capture_output=True, text=True, env=ja_env
        ).stdout
        if c_lstart == ja_lstart:
            pytest.skip("ja_JP.UTF-8 locale not available on this host; ps -o lstart= is locale-invariant here")

        monkeypatch.setenv("LANG", "ja_JP.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        result = _run_env_sh(_isolate_home)  # env は HOME/PATH のみ（LANG 無し）

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_exits_127_when_no_candidates(self, _isolate_home: Path) -> None:
        _install_env_sh(_isolate_home)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 127
        assert "could not resolve the plugin root" in result.stderr

    def test_malicious_pointer_content_is_not_executed(self, _isolate_home: Path) -> None:
        """R-01: ポインタに shell コードを書いても実行されない。

        1 行目が root、2 行目が lstart として読まれるだけで、どちらの行も
        評価・実行はされない。この内容は 2 行目（"PLE4_ROOT=/x"）が
        lstart として扱われ、実際の PID の lstart と一致しないため
        （PID 再利用相当）127 になる。
        """
        _install_env_sh(_isolate_home)
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir()
        (roots_dir / str(os.getpid())).write_text(
            "printf ROOT_FILE_EXECUTED >&2\nPLE4_ROOT=/x\n", encoding="utf-8"
        )

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
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

    def test_no_internal_variables_leak_into_caller_shell(self, tmp_path: Path, _isolate_home: Path) -> None:
        """source 後、resolver 内部の一時変数（root path や lstart を保持しうる
        ものを含む）が呼び出し元シェルに残らないこと。bootstrap 行は ``.`` で
        呼び出し元シェルに直接効くため、内部変数を unset し忘れると md 側の
        セッションを汚染する。
        """
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), pointer_root)

        leak_vars = (
            "_ple4_env_root",
            "_ple4_walk_pid",
            "_ple4_walk_depth",
            "_ple4_walk_parent",
            "_ple4_walk_candidate_pid",
            "_ple4_walk_pointer",
            "_ple4_walk_root",
            "_ple4_walk_recorded_lstart",
            "_ple4_walk_actual_lstart",
        )
        printf_args = " ".join(f'"${{{name}:-U}}"' for name in leak_vars)
        printf_fmt = "".join("[%s]" for _ in leak_vars)
        script = (
            '. "$HOME/.ple4/env.sh" || exit 127\n'
            'ple4_run marker\n'
            f"printf 'leak={printf_fmt}\\n' {printf_args}\n"
        )
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert f"leak={'[U]' * len(leak_vars)}" in result.stdout

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
        dead_entry.write_text(f"{tmp_path / 'stale-root'}\nsome-lstart\n", encoding="utf-8")
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
