"""claq.lib.env_pointer の回収（GC）と shell 側の解決のテスト。

古いポインタ・一時ファイルの回収（``_pid_is_alive`` / ``_gc_roots`` / GC の
間引き）と、生成された ``env.sh`` を実際に shell で source して祖先 walk・
``lstart`` 一致判定・PID 再利用の拒否・全滅時の 127・悪意あるポインタ内容が
実行されないことを実挙動として検証する（``env-template.sh`` は shell ファイルの
ため ``--cov`` の対象外であり、テキスト assert だけでは分岐が検証されない）。
共有する補助と HOME 隔離の autouse フィクスチャは ``_support.py`` に置く。
"""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from pathlib import Path

import pytest

from claq.lib import env_pointer as mod
from claq.lib.constants import BASE_DIR_NAME

from ._support import _ENV_TEMPLATE, _make_plugin_root
from ._support import _isolate_home as _isolate_home


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

    def test_unicode_digit_and_huge_pid_names_do_not_raise(self, tmp_path: Path) -> None:
        """Unicode の数字や C の int に収まらない数字の名前でも例外で落ちず、猶予後に削除する。

        ここで落ちると launcher が hook を実行する前に終わり、保護フックが素通りになる。
        """
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        for name in ("²", "9" * 40):
            entry = roots_dir / name
            entry.write_text("/old\n", encoding="utf-8")
            os.utime(entry, (old, old))

        mod._gc_roots(roots_dir)

        assert list(roots_dir.iterdir()) == []

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
        """別 writer が rename 直前に作った一時ファイルを race で消さない。

        ``roots/`` で実際に発生しうるのは ``<name>.tmp.<random>``
        （mkstemp）。旧 writer 残骸（``<pid>.tmp.<writer_pid>``）と
        ``env.sh.tmp.<pid>`` も
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

    def test_fresh_env_sh_tmp_in_claq_dir_survives_gc(self, tmp_path: Path) -> None:
        """claq_dir 直下の新鮮な env.sh.tmp.* は age-gate で残る。"""
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        fresh = claq_dir / f"{mod._ENV_TMP_PREFIX}freshxxx"
        fresh.write_text("in-flight\n", encoding="utf-8")

        mod._gc_stale_temp_files(claq_dir, time.time())

        assert fresh.exists()

    def test_stale_env_sh_tmp_in_claq_dir_is_removed(self, tmp_path: Path) -> None:
        """猶予を過ぎた env.sh.tmp.* は claq_dir から回収する。"""
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        stale = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(stale, (old, old))

        mod._gc_stale_temp_files(claq_dir, time.time())

        assert not stale.exists()

    def test_gc_env_sh_tmp_does_not_touch_env_sh_or_mem_db(self, tmp_path: Path) -> None:
        """プレフィックス不一致の state ファイルは env.sh tmp GC の対象外。

        survivor も stale tmp と同じ古い mtime にする。新鮮なまま残すと、
        プレフィックスを ``env.sh`` まで広げた実装でもこのテストは緑になる。
        """
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        env_sh = claq_dir / mod._ENV_FILENAME
        mem_db = claq_dir / "mem.db"
        other = claq_dir / "unrelated.txt"
        env_sh.write_text("keep-env\n", encoding="utf-8")
        mem_db.write_text("keep-db\n", encoding="utf-8")
        other.write_text("keep-other\n", encoding="utf-8")
        stale = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        for path in (env_sh, mem_db, other, stale):
            os.utime(path, (old, old))

        mod._gc_stale_temp_files(claq_dir, time.time())

        assert not stale.exists()
        assert env_sh.read_text(encoding="utf-8") == "keep-env\n"
        assert mem_db.read_text(encoding="utf-8") == "keep-db\n"
        assert other.read_text(encoding="utf-8") == "keep-other\n"

    def test_unlistable_claq_dir_is_noop_for_env_sh_tmp_gc(self, tmp_path: Path) -> None:
        """claq_dir が列挙できなくても env.sh tmp GC は例外を送出しない。"""
        mod._gc_stale_temp_files(tmp_path / "does-not-exist", time.time())

    def test_env_sh_tmp_stat_failure_is_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env.sh.tmp.* の stat 失敗は無視する。"""
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        entry = claq_dir / f"{mod._ENV_TMP_PREFIX}statfail"
        entry.write_text("x\n", encoding="utf-8")
        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)
        mod._gc_stale_temp_files(claq_dir, time.time())
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
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir()
        legacy = roots_dir / "latest"
        legacy.write_text("/old\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(legacy, (old, old))
        stale_env_tmp = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale_env_tmp.write_text("orphan\n", encoding="utf-8")
        os.utime(stale_env_tmp, (old, old))

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()
        assert not stale_env_tmp.exists()
        assert (claq_dir / mod._GC_STAMP_FILENAME).exists()

    def test_gc_skipped_when_stamp_is_fresh(self, tmp_path: Path) -> None:
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir()
        (claq_dir / mod._GC_STAMP_FILENAME).touch()
        (roots_dir / "latest").write_text("/old\n", encoding="utf-8")
        stale_env_tmp = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale_env_tmp.write_text("orphan\n", encoding="utf-8")
        old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(stale_env_tmp, (old, old))

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert (roots_dir / "latest").exists()  # throttle により GC は走らない
        assert stale_env_tmp.exists()

    def test_gc_runs_when_stamp_is_stale(self, tmp_path: Path) -> None:
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir()
        stamp = claq_dir / mod._GC_STAMP_FILENAME
        stamp.touch()
        stamp_old = time.time() - mod._GC_THROTTLE_SECONDS - 60
        os.utime(stamp, (stamp_old, stamp_old))
        legacy = roots_dir / "latest"
        legacy.write_text("/old\n", encoding="utf-8")
        legacy_old = time.time() - mod._DEAD_PID_GRACE_SECONDS - 60
        os.utime(legacy, (legacy_old, legacy_old))

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()

    def test_stamp_stat_failure_is_treated_as_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir()

        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            if self.name == mod._GC_STAMP_FILENAME:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())  # 例外を送出しないことを確認

    def test_stamp_touch_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir()

        def _boom_touch(self: Path, *args: object, **kwargs: object) -> None:
            raise OSError("boom")

        monkeypatch.setattr(Path, "touch", _boom_touch)

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())  # GC 自体は実行される（touch 失敗は無視）


def _run_env_sh(
    home: Path, *, script: str = '. "$HOME/.claq/env.sh" || exit 127\nclaq_run marker\n'
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
    """``home/.claq/env.sh`` にテンプレートを置く（``roots/`` は各テストが用意する）。"""
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

        script = 'sh -c \'. "$HOME/.claq/env.sh" || exit 127; claq_run marker\'\n'
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_ancestor_beyond_depth_limit_exits_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """3 段祖先（walk 上限 2 段の外側）にしかポインタが無ければ、推測せず 127 にする。

        writer は host インスタンス専有の 2 段（bash tool の shell とその親）
        にしか書かない設計（共有祖先での衝突を構造的に起こさないため）。resolver
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
        script_file.write_text('. "$HOME/.claq/env.sh" || exit 127\nclaq_run marker\n', encoding="utf-8")

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
            'printf "not-a-real-root\\nintentionally-wrong-lstart\\n" > "$HOME/.claq/roots/$$"\n'
            'sh -c \'. "$HOME/.claq/env.sh" || exit 127; claq_run marker\'\n'
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
            'printf "" > "$HOME/.claq/roots/$$"\n'
            'sh -c \'. "$HOME/.claq/env.sh" || exit 127; claq_run marker\'\n'
        )
        result = _run_env_sh(_isolate_home, script=script)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_two_unrelated_alive_hosts_exit_127(self, tmp_path: Path, _isolate_home: Path) -> None:
        """祖先ではない 2 つの実在プロセスの root が対立していても、
        当て推量せず 127 にする（root 値合意のような fallback で片方を
        静かに採用しない）。
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
        """ポインタに shell コードを書いても実行されない。

        1 行目が root、2 行目が lstart として読まれるだけで、どちらの行も
        評価・実行はされない。この内容は 2 行目（"CLAQ_ROOT=/x"）が
        lstart として扱われ、実際の PID の lstart と一致しないため
        （PID 再利用相当）127 になる。
        """
        _install_env_sh(_isolate_home)
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir()
        (roots_dir / str(os.getpid())).write_text(
            "printf ROOT_FILE_EXECUTED >&2\nCLAQ_ROOT=/x\n", encoding="utf-8"
        )

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
        assert result.returncode == 127

    def test_root_with_special_characters_resolves(self, tmp_path: Path, _isolate_home: Path) -> None:
        """$・空白・二重引用符・backtick を含む root がシェル展開されず解決できる。"""
        _install_env_sh(_isolate_home)
        odd_root = _make_plugin_root(tmp_path / 'plugin$var with space and "quote`tick')
        _write_pointer(_isolate_home / BASE_DIR_NAME / "roots", os.getpid(), odd_root)

        result = _run_env_sh(_isolate_home)

        assert result.returncode == 0, result.stderr
        assert "ran:marker" in result.stdout

    def test_symlink_pointer_is_rejected(self, tmp_path: Path, _isolate_home: Path) -> None:
        """symlink であること**だけ**を理由に拒否されること（``[ -L ... ] ||`` ガード）。

        symlink 先の中身を 1 行（root のみ・lstart 行なし）にすると、
        ``env-template.sh`` の ``-L`` ガードを削除しても後段の空 lstart
        チェックが 127 を返し、緑のまま通る —— ``-L`` ガードを実行させるには
        「有効な 2 行を持つ symlink」を置く必要がある。

        ここでは symlink 先を通常ファイルとして置けば解決できる内容
        （``root\\n<自 PID の実 lstart>\\n``）にし、同じ内容を通常ファイルとして
        置いた場合は解決できることを陽性対照として先に確認する。両者の差は
        symlink かどうかだけなので、127 の理由が ``-L`` ガードに特定される。
        """
        _install_env_sh(_isolate_home)
        pointer_root = _make_plugin_root(tmp_path / "pointer-root")
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        pointer_path = roots_dir / str(os.getpid())

        # 陽性対照: まったく同じ内容を通常ファイルとして置けば解決できる。
        _write_pointer(roots_dir, os.getpid(), pointer_root)
        valid_content = pointer_path.read_text(encoding="utf-8")
        assert _run_env_sh(_isolate_home).returncode == 0

        # 本題: 内容は同一のまま、実体を symlink に差し替える。
        real_pointer = tmp_path / "real-pointer"
        real_pointer.write_text(valid_content, encoding="utf-8")
        pointer_path.unlink()
        pointer_path.symlink_to(real_pointer)

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
            "_claq_env_root",
            "_claq_walk_pid",
            "_claq_walk_depth",
            "_claq_walk_parent",
            "_claq_walk_candidate_pid",
            "_claq_walk_pointer",
            "_claq_walk_root",
            "_claq_walk_recorded_lstart",
            "_claq_walk_actual_lstart",
        )
        printf_args = " ".join(f'"${{{name}:-U}}"' for name in leak_vars)
        printf_fmt = "".join("[%s]" for _ in leak_vars)
        script = (
            '. "$HOME/.claq/env.sh" || exit 127\n'
            'claq_run marker\n'
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
        評価されないことが本質。この防御はデータ形式そのものにあるため、
        結果が 0/127 のどちらでも良く、実行痕跡が無いことだけを検証する）。
        """
        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        roots_dir.mkdir(parents=True)
        (roots_dir / str(os.getpid())).write_bytes(b"printf ROOT_FILE_EXECUTED >&2\x00/tmp/x\n")
        _install_env_sh(_isolate_home)

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
