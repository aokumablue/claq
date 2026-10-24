"""claq.lib.env_pointer の書き込み側のテスト。

``write_env_pointer`` の Python 側ロジック（祖先チェーンの解決・原子的書き込み・
内容が同じ場合の書き込み省略・失敗時の握り潰しと警告・state dir の解決）を検証する。
ポインタの回収（GC）と、生成された ``env.sh`` を実際に shell で source する検証は
``test_env_pointer_gc_and_resolve.py`` にある。共有する補助と HOME 隔離の
autouse フィクスチャは ``_support.py`` に置く。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from claq.lib import env_pointer as mod
from claq.lib.constants import BASE_DIR_NAME

from ._support import _ENV_TEMPLATE, _make_plugin_root
from ._support import _isolate_home as _isolate_home


@pytest.fixture(autouse=True)
def _reset_warned(monkeypatch: pytest.MonkeyPatch) -> None:
    """プロセス 1 回きりの警告フラグを各テストの開始時に未警告へ戻す。"""
    monkeypatch.setattr(mod, "_warned_this_process", False)


def _raise(exc: BaseException) -> Callable[..., None]:
    """呼ばれたら ``exc`` を送出する差し替え関数を返す。"""

    def _boom(*_args: object, **_kwargs: object) -> None:
        """差し替え先で ``exc`` を送出する。"""
        raise exc

    return _boom


def _pointer_file(home: Path) -> Path:
    """直接 PPID のポインタファイルの場所を返す。"""
    return home / BASE_DIR_NAME / "roots" / str(os.getppid())


def _single_warning(err: str) -> dict[str, object]:
    """stderr がちょうど 1 行の JSON 警告であることを確かめ、その内容を返す。"""
    lines = [line for line in err.splitlines() if line]
    assert len(lines) == 1
    return json.loads(lines[0])


class TestWriteEnvPointer:
    """write_env_pointer の Python 側ロジックのテスト。"""

    def test_writes_roots_and_env_sh_as_plain_data(self, tmp_path: Path, _isolate_home: Path) -> None:
        """roots/<ppid> は shell 代入形式ではなく root + lstart の 2 行のみであること。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        claq_dir = _isolate_home / BASE_DIR_NAME
        pid_file = _pointer_file(_isolate_home)
        lines = pid_file.read_text(encoding="utf-8").split("\n")
        assert lines[0] == str(plugin_root)
        assert lines[1] != ""  # lstart が記録されている
        assert "CLAQ_ROOT" not in pid_file.read_text(encoding="utf-8")
        assert (claq_dir / "env.sh").read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")
        assert oct(claq_dir.stat().st_mode)[-3:] == "700"
        assert oct((claq_dir / "roots").stat().st_mode)[-3:] == "700"

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

        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        claq_dir = _isolate_home / BASE_DIR_NAME
        assert called == []
        assert [p.name for p in (claq_dir / "roots").iterdir() if p.name.isdigit()] == []
        assert (claq_dir / "env.sh").is_file()

    def test_writes_ancestor_chain_not_just_direct_ppid(self, tmp_path: Path, _isolate_home: Path) -> None:
        """直接 PPID だけでなく祖先にも書く（実チェーンでの下限確認）。

        実測段数は環境依存（pytest プロセスの親が既に init/1 なら 1 段で打ち切られる）ので、
        実チェーンでは「直接 PPID が必ず書かれる」ことと「``_MAX_ANCESTOR_DEPTH`` を超えない」
        ことだけを見る。「2 段目も確かに書かれる」ことは合成チェーンを与える
        :meth:`test_writes_every_pid_in_the_resolved_ancestor_chain` が担う。
        """
        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        written = {p.name for p in roots_dir.iterdir() if p.name.isdigit()}
        assert str(os.getppid()) in written
        assert len(written) <= mod._MAX_ANCESTOR_DEPTH

    def test_writes_every_pid_in_the_resolved_ancestor_chain(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解決したチェーンの全要素が、正しい内容で ``roots/<pid>`` に書かれること。

        合成した 2 要素チェーンで、ファイル名の集合と各ファイルの中身まで見る（``chain[:1]``
        への退行も、同じ pid を 2 回書く実装も捕まえる）。探索段数には ``_MAX_ANCESTOR_DEPTH``
        そのものが渡され、その定数は 2 以上（直接 PPID だけでは足りないという仕様）であること。
        実チェーンの段数は環境依存で下限を表明できないため、定数側を固定して退行を止める。
        """
        chain = [(4242, "Mon Jan  1 00:00:00 2035"), (4243, "Tue Jan  2 00:00:00 2035")]
        depths: list[int] = []
        monkeypatch.setattr(mod, "_resolve_ancestor_chain", lambda depth: depths.append(depth) or list(chain))
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        assert mod._MAX_ANCESTOR_DEPTH == 2
        assert depths == [mod._MAX_ANCESTOR_DEPTH]
        assert {p.name for p in roots_dir.iterdir() if p.name.isdigit()} == {"4242", "4243"}
        for pid, lstart in chain:
            assert (roots_dir / str(pid)).read_text(encoding="utf-8") == f"{plugin_root}\n{lstart}\n"

    @pytest.mark.parametrize("second", ["first", "second"], ids=["same-root", "conflicting-root"])
    def test_repeated_call_overwrites_with_the_latest_root(
        self, tmp_path: Path, _isolate_home: Path, second: str
    ) -> None:
        """再度呼ぶとポインタは最後の root で上書きされる（同一 root は自己修復、別 root も poison しない）。

        writer が書く祖先 PID は host インスタンス専有（最大 2 段）で他 host と共有されないため、
        同一 PID に別の root が観測されるのは同一 host のアップグレードだけで、上書きが正しい。
        """
        mod.write_env_pointer(_make_plugin_root(tmp_path / "first"))
        mod.write_env_pointer(_make_plugin_root(tmp_path / second))

        assert _pointer_file(_isolate_home).read_text(encoding="utf-8").split("\n")[0] == str(tmp_path / second)

    def test_rejects_root_containing_newline(self, tmp_path: Path, _isolate_home: Path) -> None:
        """改行を含む root は書き込まれず、既存の有効ポインタも壊されない。"""
        mod.write_env_pointer(_make_plugin_root(tmp_path / "good"))
        pid_file = _pointer_file(_isolate_home)
        before = pid_file.read_text(encoding="utf-8")

        mod.write_env_pointer(tmp_path / "evil\nCLAQ_INJECTED=1")  # 例外を送出しない（握り潰す）

        assert pid_file.read_text(encoding="utf-8") == before

    def test_swallows_missing_template(self, tmp_path: Path, _isolate_home: Path) -> None:
        """env-template.sh が無くても例外を送出しない（hook を壊さない）。"""
        plugin_root = tmp_path / "plugin-without-template"
        plugin_root.mkdir()

        mod.write_env_pointer(plugin_root)

        assert not (_isolate_home / BASE_DIR_NAME / "env.sh").exists()

    @pytest.mark.parametrize(
        ("target", "name", "reason"),
        [
            pytest.param(mod, "_ensure_private_dir", "permission denied", id="private-dir-unwritable"),
            pytest.param(mod.tempfile, "mkstemp", "mkstemp failed", id="mkstemp-oserror"),
        ],
    )
    def test_write_failure_warns_once_per_process(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        target: object,
        name: str,
        reason: str,
    ) -> None:
        """書き込み失敗は例外を送出せず、stderr へ 1 回だけ JSON 警告を出す（無音の機能低下を防ぐ）。"""
        monkeypatch.setattr(target, name, _raise(OSError(reason)))
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)
        mod.write_env_pointer(plugin_root)

        payload = _single_warning(capsys.readouterr().err)
        assert payload["claqEnvPointerWriteFailed"] is True
        assert reason in str(payload["reason"])

    def test_warn_once_swallows_stderr_write_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """stderr への書き込み自体が失敗しても例外を送出しない。"""

        class _BoomStderr:
            """write で OSError を送出する stderr。"""

            def write(self, _text: str) -> int:
                """書き込み失敗を模す。"""
                raise OSError("broken pipe")

        monkeypatch.setattr(mod.sys, "stderr", _BoomStderr())

        mod._warn_once("reason")

    def test_warn_once_is_noop_on_second_call(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """警告済みなら 2 回目以降の呼び出しでは何も書かない（早期 return の分岐）。"""
        monkeypatch.setattr(mod, "_warned_this_process", True)

        mod._warn_once("reason")

        assert capsys.readouterr().err == ""

    def test_just_written_pointers_survive_gc_even_if_pids_look_dead(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """祖先チェーン全体が使い捨てプロセスでも、同じ呼び出しで書いたポインタを GC が消さないこと。

        PID が全て不在かつ猶予切れの状況を強制し、``_gc_roots`` の ``keep_pids`` 除外が
        ``write_env_pointer`` 経由の統合シナリオで実際に効くことを確認する回帰テスト。
        """
        monkeypatch.setattr(mod, "_pid_is_alive", lambda _pid: False)
        monkeypatch.setattr(mod, "_DEAD_PID_GRACE_SECONDS", 0)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        assert _pointer_file(_isolate_home).read_text(encoding="utf-8").split("\n")[0] == str(plugin_root)


class TestStateHome:
    """_state_home の解決順とフォールバック分岐のテスト。"""

    @pytest.mark.parametrize("claq_home", [None, "other"], ids=["home-set", "claq-home-ignored"])
    def test_uses_home_env_and_ignores_claq_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claq_home: str | None
    ) -> None:
        """HOME があればそれを使い、テスト隔離専用の CLAQ_HOME は state dir に影響しない。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        if claq_home is not None:
            monkeypatch.setenv("CLAQ_HOME", str(tmp_path / claq_home))
        assert mod._state_home() == tmp_path

    def test_falls_back_to_path_home_when_unset(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """HOME 未設定なら Path.home() を使う。"""
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.setattr(mod.Path, "home", classmethod(lambda cls: tmp_path))
        assert mod._state_home() == tmp_path

    def test_write_warns_when_home_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """HOME を決められないときは cwd へ書かず、警告して終わる（シェル側は $HOME しか読まない）。"""
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(mod.Path, "home", classmethod(_raise(RuntimeError("no home"))))

        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        assert not (tmp_path / BASE_DIR_NAME).exists()
        assert _single_warning(capsys.readouterr().err)["claqEnvPointerWriteFailed"] is True


class TestAtomicWrite:
    """_atomic_write_text の原子性・失敗時挙動のテスト。"""

    @pytest.fixture
    def target(self, tmp_path: Path) -> Path:
        """空の作業ディレクトリ内の書き込み先を返す。"""
        workdir = tmp_path / "workdir"
        workdir.mkdir()
        return workdir / "out.txt"

    def test_success_uses_random_tmp_name_and_leaves_nothing_behind(
        self, target: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """成功時は ``<name>.tmp.<random>``（PID 固定でも数字のみでもない）を経由し、tmp を残さない。"""
        seen: list[str] = []
        real_replace = os.replace

        def _capture(src: object, dst: object) -> None:
            """rename 元のファイル名を記録して本物の replace へ渡す。"""
            seen.append(Path(str(src)).name)
            real_replace(src, dst)

        monkeypatch.setattr(mod.os, "replace", _capture)

        mod._atomic_write_text(target, "content\n")

        assert target.read_text(encoding="utf-8") == "content\n"
        assert [p for p in target.parent.iterdir() if p != target] == []
        assert len(seen) == 1
        prefix = f"{target.name}.tmp."
        assert seen[0].startswith(prefix)
        suffix = seen[0][len(prefix) :]
        assert suffix
        assert suffix != str(os.getpid())
        assert not seen[0].isdigit()

    @pytest.mark.parametrize(("unlink_fails", "leftovers"), [(False, 0), (True, 1)], ids=["unlink-ok", "unlink-fails"])
    def test_replace_failure_raises_and_cleans_up_tmp(
        self, target: Path, monkeypatch: pytest.MonkeyPatch, unlink_fails: bool, leftovers: int
    ) -> None:
        """rename 失敗は元の OSError を送出し、tmp を消す（その削除自体の失敗は握り潰す）。"""
        monkeypatch.setattr(mod.os, "replace", _raise(OSError("replace failed")))
        if unlink_fails:
            monkeypatch.setattr(Path, "unlink", _raise(OSError("unlink failed")))

        with pytest.raises(OSError, match="replace failed"):
            mod._atomic_write_text(target, "content\n")

        assert not target.exists()
        assert len(list(target.parent.iterdir())) == leftovers

    @pytest.mark.parametrize("close_fails", [False, True], ids=["close-ok", "close-fails"])
    def test_fdopen_failure_closes_fd_and_removes_tmp(
        self, target: Path, monkeypatch: pytest.MonkeyPatch, close_fails: bool
    ) -> None:
        """fdopen が失敗したら mkstemp の fd を閉じて tmp を残さず、close 自体の失敗も元の OSError で返す。"""
        closed: list[int] = []
        real_close = os.close

        def _close(fd: int) -> None:
            """fd を記録して本物の close を呼び、指定時はその後に失敗を模す。"""
            closed.append(fd)
            real_close(fd)
            if close_fails:
                raise OSError("close failed")

        monkeypatch.setattr(mod.os, "fdopen", _raise(OSError("fdopen failed")))
        monkeypatch.setattr(mod.os, "close", _close)

        with pytest.raises(OSError, match="fdopen failed"):
            mod._atomic_write_text(target, "content\n")

        assert not target.exists()
        assert list(target.parent.iterdir()) == []
        assert closed

    @pytest.mark.parametrize("as_symlink", [True, False], ids=["symlink-bait", "stale-regular-file"])
    def test_precreated_pid_named_tmp_is_left_alone(self, target: Path, as_symlink: bool) -> None:
        """予測可能な ``<name>.tmp.<pid>`` が symlink でも stale な実ファイルでも、触らずに別名で書く。"""
        bait = target.parent / f"{target.name}.tmp.{os.getpid()}"
        if as_symlink:
            victim = target.parent / "victim.txt"
            victim.write_text("untouched\n", encoding="utf-8")
            bait.symlink_to(victim)
        else:
            bait.write_text("untouched\n", encoding="utf-8")

        mod._atomic_write_text(target, "new\n")

        assert target.read_text(encoding="utf-8") == "new\n"
        assert bait.read_text(encoding="utf-8") == "untouched\n"
        assert bait.is_symlink() is as_symlink

    def test_same_process_concurrent_writes_leave_complete_file(self, target: Path) -> None:
        """barrier 同期した複数スレッドが同じ target を書いても完全なレコードだけが残る。"""
        payloads = [f"payload-{i:03d}\n" for i in range(8)]
        barrier = threading.Barrier(len(payloads), timeout=5)
        errors: list[BaseException] = []

        def _write(text: str) -> None:
            """全スレッドの到着を待ってから書く。"""
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
        assert [path for path in target.parent.iterdir() if path != target] == []


class TestWriteTextIfChanged:
    """_write_text_if_changed の分岐と、それが env.sh 経路へ効くことのテスト。"""

    @pytest.mark.parametrize(
        "existing",
        [None, b"old\n", b"\xff\xfe not utf-8\n"],
        ids=["missing", "different-content", "non-utf8-existing"],
    )
    def test_writes_when_content_differs(self, tmp_path: Path, existing: bytes | None) -> None:
        """未作成・内容違い・非 UTF-8 の既存ファイルはいずれも書く。

        非 UTF-8 を `read_text` で比較すると `UnicodeDecodeError`（`ValueError` 系）になり、
        `write_env_pointer` の `OSError` ハンドラを素通りしてフックごと落ちる。バイト比較であることを固定する。
        """
        target = tmp_path / "env.sh"
        if existing is not None:
            target.write_bytes(existing)

        assert mod._write_text_if_changed(target, "body\n") is True
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_identical_content_skips_the_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容が一致していれば `_atomic_write_text` を呼ばない。

        「書いた結果が同じ」ではなく「書きに行っていない」ことを見る。前者は
        無条件書き込みでも成立するため、fsync を省いた証拠にならない。
        """
        target = tmp_path / "env.sh"
        target.write_text("body\n", encoding="utf-8")
        monkeypatch.setattr(mod, "_atomic_write_text", _raise(AssertionError("同一内容で書き込みが走った")))

        assert mod._write_text_if_changed(target, "body\n") is False

    def test_unreadable_file_falls_through_to_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """読み取りに失敗したら「一致していない」として書きに行く。"""
        target = tmp_path / "env.sh"
        target.write_text("body\n", encoding="utf-8")
        monkeypatch.setattr(Path, "read_bytes", _raise(OSError("unreadable")))

        assert mod._write_text_if_changed(target, "body\n") is True

    def test_symlink_with_identical_content_is_replaced(self, tmp_path: Path) -> None:
        """内容が一致していても symlink なら実体ファイルへ戻す。

        `os.replace` による書き込みには「`env.sh` が別の場所へ向けられていたら実体へ戻す」
        自己修復が付随する。内容一致だけで省くとこの性質が静かに失われるため、symlink は比較の前に除外する。
        """
        real = tmp_path / "elsewhere.sh"
        real.write_text("body\n", encoding="utf-8")
        target = tmp_path / "env.sh"
        target.symlink_to(real)

        assert mod._write_text_if_changed(target, "body\n") is True
        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_second_write_env_pointer_call_does_not_touch_env_sh(self, tmp_path: Path, _isolate_home: Path) -> None:
        """2 回目以降の `write_env_pointer` は env.sh を書き直さない。

        PreToolUse は全ツール呼び出しで発火するため、内容が変わらない env.sh の書き直しは
        1 呼び出しあたり回避可能な fsync 1 本になる。inode と mtime が据え置きであることで
        「書いていない」ことを示す。
        """
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        mod.write_env_pointer(plugin_root)
        env_sh = _isolate_home / BASE_DIR_NAME / mod._ENV_FILENAME
        before = env_sh.stat()
        os.utime(env_sh, (before.st_atime - 100, before.st_mtime - 100))
        stale = env_sh.stat()

        mod.write_env_pointer(plugin_root)

        after = env_sh.stat()
        assert (after.st_ino, after.st_mtime) == (stale.st_ino, stale.st_mtime)
        assert env_sh.read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")

    def test_changed_template_is_written_on_the_next_call(self, tmp_path: Path, _isolate_home: Path) -> None:
        """テンプレートが変われば（プラグイン更新）次の呼び出しで反映されること。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        mod.write_env_pointer(plugin_root)

        (plugin_root / "runtime" / "env-template.sh").write_text("# v2\n", encoding="utf-8")
        mod.write_env_pointer(plugin_root)

        assert (_isolate_home / BASE_DIR_NAME / mod._ENV_FILENAME).read_text(encoding="utf-8") == "# v2\n"


class _PsResult:
    """``subprocess.run`` の戻り値の代わりに使う最小の結果。"""

    def __init__(self, returncode: int, stdout: str) -> None:
        """終了コードと標準出力を保持する。"""
        self.returncode = returncode
        self.stdout = stdout


class TestResolveAncestorChain:
    """_resolve_ancestor_chain の祖先探索ロジックのテスト。"""

    def test_real_chain_includes_own_ppid(self) -> None:
        """モックなしの実行で、チェーンの先頭が os.getppid() と一致すること。"""
        chain = mod._resolve_ancestor_chain(mod._MAX_ANCESTOR_DEPTH)
        assert chain
        assert chain[0][0] == os.getppid()
        assert chain[0][1] != ""

    @pytest.mark.parametrize(
        "exc",
        [FileNotFoundError("ps not found"), mod.subprocess.TimeoutExpired(cmd="ps", timeout=5)],
        ids=["ps-missing", "ps-timeout"],
    )
    def test_empty_when_ps_cannot_run(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        """`ps` の起動失敗・タイムアウトは空チェーン（fail-open）。"""
        monkeypatch.setattr(mod.subprocess, "run", _raise(exc))
        assert mod._resolve_ancestor_chain(4) == []

    @pytest.mark.parametrize(
        ("returncode", "table", "depth", "expected"),
        [
            pytest.param(1, "", 4, [], id="ps-nonzero"),
            pytest.param(
                0,
                "{ppid} 5000 Thu Aug 20 10:00:00 2026\n5000 6000 Thu Aug 20 09:00:00 2026\n"
                "6000 7000 Thu Aug 20 08:00:00 2026\n7000 1 Thu Aug 20 07:00:00 2026\n1 0 Thu Aug 20 00:00:00 2026\n",
                4,
                [
                    ("{ppid}", "Thu Aug 20 10:00:00 2026"),
                    (5000, "Thu Aug 20 09:00:00 2026"),
                    (6000, "Thu Aug 20 08:00:00 2026"),
                    (7000, "Thu Aug 20 07:00:00 2026"),
                ],
                id="walks-synthetic-table",
            ),
            pytest.param(
                0,
                "{ppid} 5000 t0\n5000 6000 t1\n6000 7000 t2\n7000 8000 t3\n8000 9000 t4\n",
                2,
                [("{ppid}", "t0"), (5000, "t1")],
                id="stops-at-max-depth",
            ),
            pytest.param(0, "999999999 1 t0\n", 4, [], id="own-ppid-missing-from-table"),
            # int 変換失敗・フィールド不足・ppid が int でない行。own_ppid 自体の行が壊れていてチェーンを組めない
            pytest.param(0, "not-a-pid ppid lstart\n123\n{ppid} notanint t0\n", 4, [], id="malformed-lines-skipped"),
        ],
    )
    def test_chain_from_process_table(
        self,
        monkeypatch: pytest.MonkeyPatch,
        returncode: int,
        table: str,
        depth: int,
        expected: list[tuple[int | str, str]],
    ) -> None:
        """合成したプロセス表（``{ppid}`` は os.getppid()）から祖先チェーンと各 lstart を正しく組むこと。"""
        own_ppid = os.getppid()
        stdout = table.replace("{ppid}", str(own_ppid))
        monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _PsResult(returncode, stdout))

        chain = mod._resolve_ancestor_chain(depth)

        assert chain == [(own_ppid if pid == "{ppid}" else pid, lstart) for pid, lstart in expected]


class TestWriteAncestorPointer:
    """_write_ancestor_pointer の分岐のテスト。"""

    @pytest.mark.parametrize(
        "writes",
        [
            [("/root/a", "lstart-a")],
            [("/root/a", "lstart-a"), ("/root/a", "lstart-a-updated")],
            [("/root/a", "lstart-a"), ("/root/b", "lstart-b")],
        ],
        ids=["new-pointer", "refresh-matching-root", "conflicting-root-overwrites"],
    )
    def test_last_write_wins(self, tmp_path: Path, writes: list[tuple[str, str]]) -> None:
        """常に最後に書いた root と lstart の 2 行になる（衝突しても poison しない）。"""
        path = tmp_path / "42"
        for root, lstart in writes:
            mod._write_ancestor_pointer(path, root, lstart)
        root, lstart = writes[-1]
        assert path.read_text(encoding="utf-8") == f"{root}\n{lstart}\n"

    def test_write_failure_propagates(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """書き込み失敗の OSError はそのまま呼び出し元へ伝わる。"""
        monkeypatch.setattr(mod, "_atomic_write_text", _raise(OSError("disk full")))

        with pytest.raises(OSError, match="disk full"):
            mod._write_ancestor_pointer(tmp_path / "42", "/root/a", "lstart-a")


def test_ps_call_routes_through_run_text() -> None:
    """`ps` 呼び出しが `subprocess.run` 直呼びになっていないこと。

    `text=True` だけで encoding を指定しないと locale 依存のデコードになり、
    `UnicodeDecodeError` が `except (OSError, SubprocessError)` を貫通して
    docstring の「例外は発生しません」が破れる。encoding 指定を集約した `run_text` を
    迂回する経路自体を残さない（残すと次の追加でまた迂回側が選ばれる）。
    """
    source = Path(mod.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(" not in source
