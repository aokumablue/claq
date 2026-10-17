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
from pathlib import Path

import pytest

from claq.lib import env_pointer as mod
from claq.lib.constants import BASE_DIR_NAME

from ._support import _ENV_TEMPLATE, _make_plugin_root
from ._support import _isolate_home as _isolate_home


class TestWriteEnvPointer:
    """write_env_pointer の Python 側ロジックのテスト。"""

    def test_writes_roots_and_env_sh_as_plain_data(self, tmp_path: Path, _isolate_home: Path) -> None:
        """roots/<ppid> は shell 代入形式ではなく root + lstart の 2 行のみであること。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        claq_dir = _isolate_home / BASE_DIR_NAME
        roots_dir = claq_dir / "roots"
        pid_file = roots_dir / str(os.getppid())
        lines = pid_file.read_text(encoding="utf-8").split("\n")
        assert lines[0] == str(plugin_root)
        assert lines[1] != ""  # lstart が記録されている
        assert "CLAQ_ROOT" not in pid_file.read_text(encoding="utf-8")
        assert (claq_dir / "env.sh").read_text(encoding="utf-8") == _ENV_TEMPLATE.read_text(encoding="utf-8")
        assert oct(claq_dir.stat().st_mode)[-3:] == "700"
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

        claq_dir = _isolate_home / BASE_DIR_NAME
        assert called == []
        assert [p.name for p in (claq_dir / "roots").iterdir() if p.name.isdigit()] == []
        assert (claq_dir / "env.sh").is_file()

    def test_writes_ancestor_chain_not_just_direct_ppid(self, tmp_path: Path, _isolate_home: Path) -> None:
        """直接 PPID だけでなく祖先にも書く（実チェーンでの下限確認）。

        実測段数は環境依存（pytest プロセスの親が既に init/1 なら 1 段で
        打ち切られる）ので、ここでは「直接 PPID が必ず書かれる」ことと
        「``_MAX_ANCESTOR_DEPTH`` を超えない」ことだけを実チェーンで見る。
        「2 段目も確かに書かれる」ことは合成チェーンを与える
        :meth:`test_writes_every_pid_in_the_resolved_ancestor_chain` が担う
        （ここで ``1 <= len(written)`` を許すだけでは、
        ``_MAX_ANCESTOR_DEPTH = 1`` や ``chain[:1]`` への退行が緑のまま通る）。
        """
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        written = {p.name for p in roots_dir.iterdir() if p.name.isdigit()}
        assert str(os.getppid()) in written
        assert len(written) <= mod._MAX_ANCESTOR_DEPTH

    def test_max_ancestor_depth_is_more_than_the_direct_parent(self) -> None:
        """``_MAX_ANCESTOR_DEPTH`` が 2 以上に保たれていること。

        祖先チェーンに書く目的は「直接 PPID だけでは足りない」ことなので、この定数が 1 に
        戻ると仕様そのものが消える。実チェーンの段数は環境依存で下限を表明
        できないため、定数側を固定して退行を止める。
        """
        assert mod._MAX_ANCESTOR_DEPTH == 2

    def test_writes_every_pid_in_the_resolved_ancestor_chain(
        self, tmp_path: Path, _isolate_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """解決したチェーンの全要素が、正しい内容で ``roots/<pid>`` に書かれること。

        実環境のチェーンは段数が保証されないため、合成した 2 要素チェーンを与えて
        「2 件とも書かれる」ことを表明する。件数ではなくファイル名の集合と各
        ファイルの中身まで見る —— ``chain[:1]`` への退行は件数だけでも捕まるが、
        同じ pid を 2 回書く実装は件数では捕まらない。

        探索段数として ``_MAX_ANCESTOR_DEPTH`` そのものが渡されることも同時に
        確認する（リテラルを渡す実装だと定数を 1 に下げても検出できない）。
        """
        chain = [(4242, "Mon Jan  1 00:00:00 2035"), (4243, "Tue Jan  2 00:00:00 2035")]
        depths: list[int] = []

        def _fake_chain(depth: int) -> list[tuple[int, str]]:
            """探索段数を記録して合成チェーンを返す。"""
            depths.append(depth)
            return list(chain)

        monkeypatch.setattr(mod, "_resolve_ancestor_chain", _fake_chain)
        plugin_root = _make_plugin_root(tmp_path / "plugin")

        mod.write_env_pointer(plugin_root)

        roots_dir = _isolate_home / BASE_DIR_NAME / "roots"
        assert depths == [mod._MAX_ANCESTOR_DEPTH]
        assert {p.name for p in roots_dir.iterdir() if p.name.isdigit()} == {"4242", "4243"}
        for pid, lstart in chain:
            assert (roots_dir / str(pid)).read_text(encoding="utf-8") == f"{plugin_root}\n{lstart}\n"

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

        evil_root = tmp_path / "evil\nCLAQ_INJECTED=1"
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
        """~/.claq を作成できない場合も例外を送出しない。"""
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
        assert payload["claqEnvPointerWriteFailed"] is True

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
        """祖先チェーン全体が使い捨てプロセスの場合の自壊を防ぐ回帰テスト。

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

    def test_write_warns_when_home_unresolvable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """HOME を決められないときは cwd へ書かず、警告して終わる（シェル側は $HOME しか読まない）。"""
        monkeypatch.setattr(mod, "_warned_this_process", False)
        monkeypatch.delenv("HOME", raising=False)
        monkeypatch.chdir(tmp_path)

        def _boom(cls: object) -> Path:
            raise RuntimeError("no home")

        monkeypatch.setattr(mod.Path, "home", classmethod(_boom))
        mod.write_env_pointer(_make_plugin_root(tmp_path / "plugin"))

        assert not (tmp_path / BASE_DIR_NAME).exists()
        assert json.loads(capsys.readouterr().err.strip())["claqEnvPointerWriteFailed"] is True

    def test_ignores_claq_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """CLAQ_HOME はテスト隔離専用のノブであり、state dir には影響しない。"""
        other = tmp_path / "other"
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("CLAQ_HOME", str(other))
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


class TestWriteTextIfChanged:
    """_write_text_if_changed の分岐と、それが env.sh 経路へ効くことのテスト。"""

    def test_missing_file_is_written(self, tmp_path: Path) -> None:
        """未作成なら書く。"""
        target = tmp_path / "env.sh"
        assert mod._write_text_if_changed(target, "body\n") is True
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_identical_content_skips_the_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """内容が一致していれば `_atomic_write_text` を呼ばない。

        「書いた結果が同じ」ではなく「書きに行っていない」ことを見る。前者は
        無条件書き込みでも成立するため、fsync を省いた証拠にならない。
        """
        target = tmp_path / "env.sh"
        target.write_text("body\n", encoding="utf-8")

        def _fail(_path: Path, _text: str) -> None:
            """呼ばれてはいけない書き込み。"""
            raise AssertionError("同一内容で書き込みが走った")

        monkeypatch.setattr(mod, "_atomic_write_text", _fail)
        assert mod._write_text_if_changed(target, "body\n") is False

    def test_different_content_is_rewritten(self, tmp_path: Path) -> None:
        """内容が違えば書き直す。"""
        target = tmp_path / "env.sh"
        target.write_text("old\n", encoding="utf-8")
        assert mod._write_text_if_changed(target, "new\n") is True
        assert target.read_text(encoding="utf-8") == "new\n"

    def test_non_utf8_existing_file_is_rewritten(self, tmp_path: Path) -> None:
        """非 UTF-8 の既存ファイルでも例外を出さず書き直す。

        比較を `read_text` で行うと `UnicodeDecodeError`（`ValueError` 系）に
        なり、`write_env_pointer` の `OSError` ハンドラを素通りしてフックごと
        落ちる。バイト比較であることをここで固定する。
        """
        target = tmp_path / "env.sh"
        target.write_bytes(b"\xff\xfe not utf-8\n")
        assert mod._write_text_if_changed(target, "body\n") is True
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_unreadable_file_falls_through_to_write(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """読み取りに失敗したら「一致していない」として書きに行く。"""
        target = tmp_path / "env.sh"
        target.write_text("body\n", encoding="utf-8")

        def _boom(_self: Path) -> bytes:
            """読み取り不能を模す。"""
            raise OSError("unreadable")

        monkeypatch.setattr(Path, "read_bytes", _boom)
        assert mod._write_text_if_changed(target, "body\n") is True

    def test_symlink_with_identical_content_is_replaced(self, tmp_path: Path) -> None:
        """内容が一致していても symlink なら実体ファイルへ戻す。

        `os.replace` による書き込みには「`env.sh` が別の場所へ向けられて
        いたら実体へ戻す」自己修復が付随する。内容一致だけで省くとこの性質が
        静かに失われるため、symlink は比較の前に除外する。
        """
        real = tmp_path / "elsewhere.sh"
        real.write_text("body\n", encoding="utf-8")
        target = tmp_path / "env.sh"
        target.symlink_to(real)

        assert mod._write_text_if_changed(target, "body\n") is True
        assert not target.is_symlink()
        assert target.read_text(encoding="utf-8") == "body\n"

    def test_second_write_env_pointer_call_does_not_touch_env_sh(
        self, tmp_path: Path, _isolate_home: Path
    ) -> None:
        """2 回目以降の `write_env_pointer` は env.sh を書き直さない。

        PreToolUse は全ツール呼び出しで発火するため、内容が変わらない env.sh の
        書き直しは 1 呼び出しあたり回避可能な fsync 1 本になる。inode と mtime が
        据え置きであることで「書いていない」ことを示す。祖先ポインタ側は GC が
        mtime を見るので、この省略の対象外であることも同時に固定する。
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

    def test_changed_template_is_written_on_the_next_call(
        self, tmp_path: Path, _isolate_home: Path
    ) -> None:
        """テンプレートが変われば（プラグイン更新）次の呼び出しで反映されること。"""
        plugin_root = _make_plugin_root(tmp_path / "plugin")
        mod.write_env_pointer(plugin_root)
        env_sh = _isolate_home / BASE_DIR_NAME / mod._ENV_FILENAME

        (plugin_root / "runtime" / "env-template.sh").write_text("# v2\n", encoding="utf-8")
        mod.write_env_pointer(plugin_root)

        assert env_sh.read_text(encoding="utf-8") == "# v2\n"


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


def test_ps_call_routes_through_run_text() -> None:
    """`ps` 呼び出しが `subprocess.run` 直呼びになっていないこと。

    `text=True` だけで encoding を指定しないと locale 依存のデコードになり、
    `UnicodeDecodeError` が `except (OSError, SubprocessError)` を貫通して
    docstring の「例外は発生しません」が破れる。ここは子へ `LC_ALL=C` を
    強制していて `ps` の出力が ASCII になるため実害は低いが、encoding 指定を
    集約した `run_text` を迂回する経路自体を残さない（残すと「どちらが正しい
    呼び方か」が分岐し、次の追加でまた迂回側が選ばれる）。
    """
    source = Path(mod.__file__).read_text(encoding="utf-8")

    assert "subprocess.run(" not in source
