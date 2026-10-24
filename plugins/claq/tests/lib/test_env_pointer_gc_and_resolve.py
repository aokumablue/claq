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

_PAST_GRACE = mod._DEAD_PID_GRACE_SECONDS + 60
"""PID 不在・非数字名のエントリが削除対象になる経過秒数。"""

_SOURCE_AND_RUN = '. "$HOME/.claq/env.sh" || exit 127\nclaq_run marker\n'
_NESTED_SOURCE_AND_RUN = 'sh -c \'. "$HOME/.claq/env.sh" || exit 127; claq_run marker\'\n'


def _age(path: Path, seconds: float) -> None:
    """``path`` の mtime を現在から ``seconds`` 秒前にする。"""
    old = time.time() - seconds
    os.utime(path, (old, old))


def _dead_pid() -> int:
    """終了済みの子プロセスの PID を返す。"""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _raise_oserror(*_args: object, **_kwargs: object) -> None:
    """常に OSError を送出する差し替え関数。"""
    raise OSError("boom")


class TestPidIsAlive:
    """_pid_is_alive の判定分岐のテスト。"""

    @pytest.mark.parametrize(
        ("pid", "expected"), [(os.getpid(), True), (0, False), (-1, False)], ids=["self", "zero", "negative"]
    )
    def test_pid_sign_and_self(self, pid: int, expected: bool) -> None:
        """自プロセスは生存。pid=0 は自プロセスグループへのシグナルになるため 0 以下は常に False。"""
        assert mod._pid_is_alive(pid) is expected

    def test_dead_pid_is_not_alive(self) -> None:
        """終了済みの PID は生存していない。"""
        assert mod._pid_is_alive(_dead_pid()) is False

    @pytest.mark.parametrize(
        ("exc", "expected"), [(PermissionError, True), (OSError("unexpected"), False)], ids=["permission", "other"]
    )
    def test_kill_error_decides_liveness(
        self, monkeypatch: pytest.MonkeyPatch, exc: BaseException | type[BaseException], expected: bool
    ) -> None:
        """kill の PermissionError は存在すると見なし、その他の OSError は不在と見なす。"""

        def _boom(_pid: int, _sig: int) -> None:
            """kill の失敗を模す。"""
            raise exc

        monkeypatch.setattr(mod.os, "kill", _boom)
        assert mod._pid_is_alive(12345) is expected


class TestGcRoots:
    """_gc_roots / _gc_one / _gc_stale_temp_files の削除判定のテスト。"""

    @pytest.mark.parametrize(
        ("name", "age", "keep", "survives"),
        [
            # Unicode の数字や C の int に収まらない数字の名前でも落ちない（落ちると保護フックが素通りになる）
            pytest.param("²", _PAST_GRACE, False, False, id="unicode-digit-past-grace"),
            pytest.param("9" * 40, _PAST_GRACE, False, False, id="huge-pid-past-grace"),
            # 旧形式は猶予後に削除する
            pytest.param("999999.sh", _PAST_GRACE, False, False, id="legacy-pid-sh-past-grace"),
            pytest.param("latest", _PAST_GRACE, False, False, id="legacy-latest-past-grace"),
            pytest.param("latest.sh", _PAST_GRACE, False, False, id="legacy-latest-sh-past-grace"),
            # rename 直前の一時ファイル（mkstemp・旧 writer 残骸・env.sh tmp）を race で消さない
            pytest.param("env.sh.tmp.12345", 0, False, True, id="fresh-env-sh-tmp"),
            pytest.param("555555.tmp.666666", 0, False, True, id="fresh-legacy-writer-tmp"),
            pytest.param("555555.tmp.abcdefgh", 0, False, True, id="fresh-mkstemp-tmp"),
            pytest.param("<self>", 0, False, True, id="alive-fresh"),
            # 稼働中のホストは毎 hook で mtime を更新するため、古いままなのは PID 再利用と判断する
            pytest.param("<self>", mod._MAX_ROOT_AGE_SECONDS + 3600, False, False, id="alive-past-max-age"),
            # 書き込み直後の短命プロセスは 1 サイクル猶予する
            pytest.param("<dead>", 0, False, True, id="dead-within-grace"),
            pytest.param("<dead>", _PAST_GRACE, False, False, id="dead-past-grace"),
            # 使い捨ての中間 shell に書いたばかりのポインタが同じ GC 呼び出しで自壊しない
            pytest.param("<dead>", _PAST_GRACE, True, True, id="dead-past-grace-in-keep-pids"),
            pytest.param(mod._GC_STAMP_FILENAME, _PAST_GRACE, False, True, id="gc-stamp-skipped"),
        ],
    )
    def test_gc_roots_decides_each_entry(
        self, tmp_path: Path, name: str, age: float, keep: bool, survives: bool
    ) -> None:
        """roots/ の 1 エントリが名前（``<self>`` は自 PID・``<dead>`` は不在 PID）・経過時間・keep_pids で判定されること。"""
        name = {"<self>": str(os.getpid()), "<dead>": str(_dead_pid())}.get(name, name)
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        entry = roots_dir / name
        entry.write_text("/old\n", encoding="utf-8")
        _age(entry, age)

        mod._gc_roots(roots_dir, keep_pids=frozenset({int(name)}) if keep else frozenset())

        assert entry.exists() is survives

    def test_entry_unlink_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """削除対象エントリの unlink 失敗は例外にせず、エントリは残る。"""
        roots_dir = tmp_path / "roots"
        roots_dir.mkdir()
        entry = roots_dir / "424242"
        entry.write_text("/x\n", encoding="utf-8")
        _age(entry, _PAST_GRACE)
        monkeypatch.setattr(mod, "_pid_is_alive", lambda _pid: False)
        monkeypatch.setattr(Path, "unlink", _raise_oserror)

        mod._gc_roots(roots_dir)

        assert entry.exists()

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        """列挙できないディレクトリでは roots GC も env.sh tmp GC も例外を送出しない。"""
        mod._gc_roots(tmp_path / "does-not-exist")
        mod._gc_stale_temp_files(tmp_path / "does-not-exist", time.time())

    def test_stale_temp_gc_removes_only_old_env_sh_tmp(self, tmp_path: Path) -> None:
        """claq_dir 直下では猶予切れの env.sh.tmp.* だけを回収し、新鮮な tmp とプレフィックス不一致の state は残す。

        survivor（env.sh・mem.db・無関係ファイル）も stale tmp と同じ古い mtime にする。新鮮なまま残すと、
        プレフィックスを ``env.sh`` まで広げた実装でもこのテストは緑になる。
        """
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        fresh = claq_dir / f"{mod._ENV_TMP_PREFIX}freshxxx"
        fresh.write_text("in-flight\n", encoding="utf-8")
        stale = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale.write_text("orphan\n", encoding="utf-8")
        _age(stale, _PAST_GRACE)
        kept = {claq_dir / mod._ENV_FILENAME: "keep-env\n", claq_dir / "mem.db": "keep-db\n"}
        kept[claq_dir / "unrelated.txt"] = "keep-other\n"
        for path, text in kept.items():
            path.write_text(text, encoding="utf-8")
            _age(path, _PAST_GRACE)

        mod._gc_stale_temp_files(claq_dir, time.time())

        assert fresh.exists()
        assert not stale.exists()
        assert {path: path.read_text(encoding="utf-8") for path in kept} == kept

    def test_env_sh_tmp_stat_failure_is_ignored(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """env.sh.tmp.* の stat 失敗は無視する。"""
        claq_dir = tmp_path / "bc"
        claq_dir.mkdir()
        entry = claq_dir / f"{mod._ENV_TMP_PREFIX}statfail"
        entry.write_text("x\n", encoding="utf-8")
        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            """対象エントリだけ stat を失敗させる。"""
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)
        mod._gc_stale_temp_files(claq_dir, time.time())
        assert entry.exists()


class TestGcThrottle:
    """_maybe_run_gc の throttle 挙動のテスト。"""

    @pytest.fixture
    def gc_dirs(self, tmp_path: Path) -> tuple[Path, Path, Path, Path]:
        """GC が走れば消える古い legacy ポインタと env.sh tmp を置き、(claq_dir, roots_dir, legacy, tmp) を返す。"""
        claq_dir = tmp_path / "bc"
        roots_dir = claq_dir / "roots"
        roots_dir.mkdir(parents=True)
        legacy = roots_dir / "latest"
        legacy.write_text("/old\n", encoding="utf-8")
        stale_env_tmp = claq_dir / f"{mod._ENV_TMP_PREFIX}stalexxx"
        stale_env_tmp.write_text("orphan\n", encoding="utf-8")
        for path in (legacy, stale_env_tmp):
            _age(path, _PAST_GRACE)
        return claq_dir, roots_dir, legacy, stale_env_tmp

    @pytest.mark.parametrize(
        ("stamp_age", "runs"),
        [(None, True), (0, False), (mod._GC_THROTTLE_SECONDS + 60, True)],
        ids=["stamp-absent", "stamp-fresh", "stamp-stale"],
    )
    def test_stamp_age_decides_whether_gc_runs(
        self, gc_dirs: tuple[Path, Path, Path, Path], stamp_age: float | None, runs: bool
    ) -> None:
        """stamp が無いか throttle 間隔より古ければ GC を走らせて stamp を残し、新しければ何もしない。"""
        claq_dir, roots_dir, legacy, stale_env_tmp = gc_dirs
        stamp = claq_dir / mod._GC_STAMP_FILENAME
        if stamp_age is not None:
            stamp.touch()
            _age(stamp, stamp_age)

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert legacy.exists() is not runs
        assert stale_env_tmp.exists() is not runs
        assert stamp.exists()

    def test_stamp_stat_failure_is_treated_as_absent(
        self, gc_dirs: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stamp の stat 失敗は stamp 無しとして扱い、GC を走らせる。"""
        claq_dir, roots_dir, legacy, _tmp = gc_dirs
        real_stat = Path.stat

        def _boom_stat(self: Path, *args: object, **kwargs: object) -> object:
            """stamp だけ stat を失敗させる。"""
            if self.name == mod._GC_STAMP_FILENAME:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom_stat)

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()

    def test_stamp_touch_failure_is_ignored(
        self, gc_dirs: tuple[Path, Path, Path, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """stamp の touch 失敗は無視し、GC 自体は実行される。"""
        claq_dir, roots_dir, legacy, _tmp = gc_dirs
        monkeypatch.setattr(Path, "touch", _raise_oserror)

        mod._maybe_run_gc(claq_dir, roots_dir, keep_pids=frozenset())

        assert not legacy.exists()


def _run_env_sh(home: Path, *, script: str = _SOURCE_AND_RUN) -> subprocess.CompletedProcess[str]:
    """clean shell（HOME と PATH だけの環境）で ``script`` を実行する。"""
    return subprocess.run(
        ["sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )


def _lstart_for(pid: int) -> str:
    """``pid`` の現在の起動時刻を ``LC_ALL=C`` で取得する。

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


def _write_pointer(roots_dir: Path, pid: int, root: Path) -> None:
    """``roots/<pid>`` へ ``root\\n<pid の実 lstart>\\n`` の 2 行フォーマットで書く。"""
    roots_dir.mkdir(parents=True, exist_ok=True)
    (roots_dir / str(pid)).write_text(f"{root}\n{_lstart_for(pid)}\n", encoding="utf-8")


def _assert_resolved(result: subprocess.CompletedProcess[str]) -> None:
    """resolver が root を解決して claq_run を実行できたことを確かめる。"""
    assert result.returncode == 0, result.stderr
    assert "ran:marker" in result.stdout


def _assert_unresolved(result: subprocess.CompletedProcess[str]) -> None:
    """resolver が推測せず 127 で止まったことを確かめる。"""
    assert result.returncode == 127
    assert "could not resolve the plugin root" in result.stderr


@pytest.fixture
def roots_dir(_isolate_home: Path) -> Path:
    """``~/.claq/env.sh`` にテンプレートを置き、``roots/`` の場所（未作成）を返す。"""
    claq_dir = _isolate_home / BASE_DIR_NAME
    claq_dir.mkdir(parents=True, exist_ok=True)
    (claq_dir / "env.sh").write_text(_ENV_TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8")
    return claq_dir / "roots"


class TestEnvShRealExecution:
    """生成された env.sh を実際に source し、tier の各分岐を実行検証する。

    ``sh -c`` で起動した子の $PPID はテストプロセス自身の PID になる。``os.getpid()`` に置いた
    ポインタは、直接起動なら直接 PPID、内側にもう 1 段 ``sh -c`` を挟めば祖先として読まれる。
    """

    @pytest.mark.parametrize(
        ("root_name", "script"),
        [
            pytest.param("pointer-root", _SOURCE_AND_RUN, id="direct-ppid"),
            pytest.param("pointer-root", _NESTED_SOURCE_AND_RUN, id="ancestor-when-direct-ppid-misses"),
            # 直接 PPID（中間 sh）に lstart が食い違うポインタ（PID 再利用）を書き、スキップさせる
            pytest.param(
                "pointer-root",
                'printf "not-a-real-root\\nintentionally-wrong-lstart\\n" > "$HOME/.claq/roots/$$"\n'
                + _NESTED_SOURCE_AND_RUN,
                id="pid-reuse-at-direct-ppid-falls-through",
            ),
            # 直接 PPID の空ポインタは不正データとしてスキップし、walk を続ける（read 側の防御）
            pytest.param(
                "pointer-root",
                'printf "" > "$HOME/.claq/roots/$$"\n' + _NESTED_SOURCE_AND_RUN,
                id="empty-direct-pointer-skipped",
            ),
            # $・空白・二重引用符・backtick を含む root がシェル展開されない
            pytest.param('plugin$var with space and "quote`tick', _SOURCE_AND_RUN, id="special-characters-root"),
        ],
    )
    def test_resolves_from_test_process_pointer(
        self, tmp_path: Path, _isolate_home: Path, roots_dir: Path, root_name: str, script: str
    ) -> None:
        """テストプロセスに置いた有効なポインタへ、直接または不正な直接 PPID を飛ばして解決すること。"""
        _write_pointer(roots_dir, os.getpid(), _make_plugin_root(tmp_path / root_name))

        _assert_resolved(_run_env_sh(_isolate_home, script=script))

    def test_ancestor_beyond_depth_limit_exits_127(self, tmp_path: Path, _isolate_home: Path, roots_dir: Path) -> None:
        """3 段祖先（walk 上限 2 段の外側）にしかポインタが無ければ、推測せず 127 にする。

        writer は host インスタンス専有の 2 段（bash tool の shell とその親）にしか書かない設計
        （共有祖先での衝突を構造的に起こさないため）。resolver の walk 上限も同じ 2 段である
        ことをここで固定する。深いネストの文字列エスケープを避けるため、各層を argv リストとして
        組み立て、``shlex.quote`` で 1 段ずつ包む。
        """
        # テストプロセス自身（inner から見て depth 2）にのみポインタを置く — walk 上限（depth 0/1）の外側
        _write_pointer(roots_dir, os.getpid(), _make_plugin_root(tmp_path / "pointer-root"))
        script_file = tmp_path / "depth3.sh"
        script_file.write_text(_SOURCE_AND_RUN, encoding="utf-8")

        # `; exit $?` を各層に付ける: 単一コマンドだけの `sh -c` は tail-call 最適化で自分自身を
        # exec 置換することがあり（実機で確認済み）、意図した 3 段ネストが 1〜2 段に潰れる。
        # 複数コマンドにして exec 置換を防ぎ、fork による本物の 3 段チェーンを作る。
        middle = ["sh", "-c", f"{shlex.join(['sh', str(script_file)])}; exit $?"]
        result = _run_env_sh(_isolate_home, script=f"{shlex.join(middle)}; exit $?")

        _assert_unresolved(result)

    def test_two_unrelated_alive_hosts_exit_127(self, tmp_path: Path, _isolate_home: Path, roots_dir: Path) -> None:
        """祖先ではない 2 つの実在プロセスの root が対立していても、当て推量せず 127 にする。

        root 値合意のような fallback で片方を静かに採用しない。
        """
        procs = [subprocess.Popen(["sleep", "5"]) for _ in range(2)]
        try:
            for proc, name in zip(procs, ("root-a", "root-b"), strict=True):
                _write_pointer(roots_dir, proc.pid, _make_plugin_root(tmp_path / name))

            _assert_unresolved(_run_env_sh(_isolate_home))
        finally:
            for proc in procs:
                proc.terminate()
                proc.wait()

    def test_resolves_when_writer_runs_under_non_c_locale(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """writer の ambient locale が C でなくても、resolver（事実上 C ロケール）から解決できること。

        ``ps -o lstart=`` の出力はロケール依存（``LANG=ja_JP.UTF-8`` では ``木  8/20 ...``）。
        writer/resolver 双方が ``LC_ALL=C`` を明示するため一致する必要がある —— writer 側の
        ``LC_ALL`` 指定漏れという実機で踏んだ回帰を固定する。単に ``LANG`` を設定して成功を見る
        だけでは ``LC_ALL`` 注入を外した回帰を検知できないので、先に C 版と ja 版の
        ``ps -o lstart=`` が実際に異なることを確かめ、差分が無い環境ではスキップして偽の green を防ぐ。
        """
        ja_env = {**os.environ, "LANG": "ja_JP.UTF-8"}
        ja_env.pop("LC_ALL", None)
        ja_lstart = subprocess.run(
            ["ps", "-o", "lstart=", "-p", str(os.getpid())], capture_output=True, text=True, env=ja_env
        ).stdout.rstrip("\n")
        if _lstart_for(os.getpid()) == ja_lstart:
            pytest.skip("ja_JP.UTF-8 locale not available on this host; ps -o lstart= is locale-invariant here")

        monkeypatch.setenv("LANG", "ja_JP.UTF-8")
        monkeypatch.delenv("LC_ALL", raising=False)

        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        _assert_resolved(_run_env_sh(_isolate_home))  # env は HOME/PATH のみ（LANG 無し）

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            pytest.param(None, True, id="no-candidates"),
            pytest.param(b"", True, id="empty-pointer"),
            # 1 行目が root、2 行目が lstart として読まれるだけで評価されず、lstart 不一致で 127
            pytest.param(b"printf ROOT_FILE_EXECUTED >&2\nCLAQ_ROOT=/x\n", True, id="shell-code-in-pointer"),
            # writer は NUL を書かない。読み取りがどう切れても評価されないことが本質で、終了コードは問わない
            pytest.param(b"printf ROOT_FILE_EXECUTED >&2\x00/tmp/x\n", False, id="embedded-nul"),
        ],
    )
    def test_invalid_self_pointer_is_never_executed(
        self, _isolate_home: Path, roots_dir: Path, content: bytes | None, message: bool
    ) -> None:
        """自 PID のポインタが無い・空・shell コード入りでは 127 にし、どの内容もコードとして実行しない。"""
        if content is not None:
            roots_dir.mkdir()
            (roots_dir / str(os.getpid())).write_bytes(content)

        result = _run_env_sh(_isolate_home)

        assert "ROOT_FILE_EXECUTED" not in result.stderr
        if message:
            _assert_unresolved(result)

    def test_symlink_pointer_is_rejected(self, tmp_path: Path, _isolate_home: Path, roots_dir: Path) -> None:
        """symlink であること**だけ**を理由に拒否されること（``[ -L ... ] ||`` ガード）。

        symlink 先の中身を 1 行にすると ``-L`` ガードを削除しても後段の空 lstart チェックが 127 を
        返して緑のまま通るので、symlink 先は通常ファイルなら解決できる内容にする。同じ内容を
        通常ファイルとして置けば解決できることを陽性対照として先に確かめ、127 の理由を
        ``-L`` ガードに特定する。
        """
        pointer_path = roots_dir / str(os.getpid())
        _write_pointer(roots_dir, os.getpid(), _make_plugin_root(tmp_path / "pointer-root"))
        assert _run_env_sh(_isolate_home).returncode == 0

        real_pointer = tmp_path / "real-pointer"
        real_pointer.write_text(pointer_path.read_text(encoding="utf-8"), encoding="utf-8")
        pointer_path.unlink()
        pointer_path.symlink_to(real_pointer)

        assert _run_env_sh(_isolate_home).returncode == 127

    def test_end_to_end_via_write_env_pointer(self, tmp_path: Path, _isolate_home: Path) -> None:
        """write_env_pointer が書いた env.sh をそのまま source して解決できる（統合確認）。"""
        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        _assert_resolved(_run_env_sh(_isolate_home))

    def test_no_internal_variables_leak_into_caller_shell(
        self, tmp_path: Path, _isolate_home: Path, roots_dir: Path
    ) -> None:
        """source 後、resolver 内部の一時変数（root path や lstart を保持しうるものを含む）が呼び出し元シェルに残らないこと。

        bootstrap 行は ``.`` で呼び出し元シェルに直接効くため、内部変数を unset し忘れると md 側の
        セッションを汚染する。
        """
        _write_pointer(roots_dir, os.getpid(), _make_plugin_root(tmp_path / "pointer-root"))
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
        printf_fmt = "[%s]" * len(leak_vars)

        result = _run_env_sh(_isolate_home, script=f"{_SOURCE_AND_RUN}printf 'leak={printf_fmt}\\n' {printf_args}\n")

        _assert_resolved(result)
        assert f"leak={'[U]' * len(leak_vars)}" in result.stdout

    def test_gc_removed_dead_pointer_then_resolver_exits_127(
        self, tmp_path: Path, _isolate_home: Path, roots_dir: Path
    ) -> None:
        """GC が不在 PID のポインタを実際に消し、その結果を resolver が「候補ゼロ」の 127 として扱うこと。"""
        roots_dir.mkdir()
        dead_entry = roots_dir / str(_dead_pid())
        dead_entry.write_text(f"{tmp_path / 'stale-root'}\nsome-lstart\n", encoding="utf-8")
        _age(dead_entry, _PAST_GRACE)

        mod._gc_roots(roots_dir)
        assert not dead_entry.exists()

        _assert_unresolved(_run_env_sh(_isolate_home))
