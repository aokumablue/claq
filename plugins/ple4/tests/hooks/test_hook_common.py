"""hook_common のフック出力ヘルパー（SessionStart）と
stdin 読み取りガード（TTY/タイムアウト/バイト上限）のテスト。
"""

from __future__ import annotations

import io
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from ple4.hooks import hook_common
from ple4.hooks.hook_common import (
    detach_process,
    emit_session_start_output,
    print_session_start_output,
)


def _parse_session_start(output: str) -> dict:
    """出力が有効な SessionStart hookSpecificOutput JSON かを検証して返す。"""
    payload = json.loads(output)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert "additionalContext" in inner
    return inner


class TestEmitSessionStartOutput:
    @pytest.mark.parametrize(
        "additional_context",
        [
            "",
            "simple context",
            "改行\n含む\nテキスト",
            "unicode: 日本語テスト 🐍",
            "a" * 5000,
        ],
        ids=["empty", "simple", "newlines", "unicode", "long"],
    )
    def test_returns_valid_json(self, additional_context: str) -> None:
        result = emit_session_start_output(additional_context)
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == additional_context

    def test_default_empty_context(self) -> None:
        result = emit_session_start_output()
        inner = _parse_session_start(result)
        assert inner["additionalContext"] == ""

    def test_is_string(self) -> None:
        assert isinstance(emit_session_start_output(), str)

    def test_no_trailing_newline(self) -> None:
        result = emit_session_start_output()
        assert not result.endswith("\n")


class TestPrintSessionStartOutput:
    def test_prints_to_stdout(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output("hello")
        output = buf.getvalue()
        inner = _parse_session_start(output.strip())
        assert inner["additionalContext"] == "hello"

    def test_default_empty_context(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            print_session_start_output()
        inner = _parse_session_start(buf.getvalue().strip())
        assert inner["additionalContext"] == ""


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read1(self, n: int = -1) -> bytes:
        return self._data[:n] if n >= 0 else self._data


class _FakeStdin:
    """isatty() を備えた stdin の代替オブジェクト。"""

    def __init__(self, text: str, *, tty: bool = False) -> None:
        self._tty = tty
        self.buffer = _FakeBuffer(text.encode("utf-8"))
        self.read_called = False

    def isatty(self) -> bool:
        return self._tty

    def read(self, n: int = -1) -> str:
        raise AssertionError("バイト読みでは text read を使わない")


class _QueueBuffer:
    """複数回の `.read1(n)` 呼び出しへ順番にバイト列を返すフェイク buffer。

    `_FakeBuffer` と異なり、同じデータを毎回先頭から返すのではなくキューを
    1 件ずつ消費する。実 BufferedReader が複数回の read1 で少しずつデータを
    返す（あるいは EOF で b"" を返す）挙動を模す。キューが尽きたあとは常に
    b""（EOF）を返す。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        """フェイク buffer を構築する。

        Args:
            chunks: `.read1(n)` 呼び出しごとに順に返すバイト列のリスト。

        Returns:
            なし
        """
        self._chunks = list(chunks)

    def read1(self, n: int = -1) -> bytes:
        """キューの先頭チャンクから最大 `n` バイトを返す。尽きていれば EOF（b""）。

        `n` バイト以上あれば先頭 `n` バイトだけを返し、残りは次回呼び出し用に
        キューの先頭へ戻す（実 `read1()` が要求量を超えて返さない挙動を模す）。

        Args:
            n: 呼び出し側が要求する最大バイト数。負値なら無制限。

        Returns:
            キューの次のバイト列（最大 `n` バイト）、または尽きていれば b""。
        """
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if n < 0 or n >= len(chunk):
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[n:]
        return chunk[:n]


class _QueueStdin:
    """`.buffer` に `_QueueBuffer` を持つフェイク stdin。isatty は常に False。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """フェイク stdin を構築する。

        Args:
            chunks: `.buffer.read1(n)` が順に返すバイト列のリスト。

        Returns:
            なし
        """
        self.buffer = _QueueBuffer(chunks)

    def isatty(self) -> bool:
        """常に非 TTY（パイプ接続）を表す False を返す。

        Returns:
            False
        """
        return False


class _BlockingBuffer:
    """先頭チャンクを返した後、以降の `.read1()` で永久にブロックするフェイク buffer。

    書き手が payload を送り切らないまま接続も閉じない状態を模す。ワーカー
    スレッドは daemon なので、ブロックしたまま残ってもプロセス終了は妨げない。
    """

    def __init__(self, chunks: list[bytes]) -> None:
        """フェイク buffer を構築する。

        Args:
            chunks: `.read1()` が順に返すバイト列。尽きた後はブロックする。

        Returns:
            なし
        """
        self._chunks = list(chunks)
        self._blocked = threading.Event()

    def read1(self, n: int = -1) -> bytes:
        """次のチャンクを返す。尽きていれば永久にブロックする。

        Args:
            n: 要求する最大バイト数。

        Returns:
            次のチャンク（最大 `n` バイト）。

        Raises:
            例外は発生しません。
        """
        if not self._chunks:
            self._blocked.wait()
            return b""
        chunk = self._chunks.pop(0)
        return chunk if n < 0 or n >= len(chunk) else chunk[:n]


class _BlockingStdin:
    """`.buffer` に `_BlockingBuffer` を持つフェイク stdin。isatty は常に False。"""

    def __init__(self, chunks: list[bytes]) -> None:
        """フェイク stdin を構築する。

        Args:
            chunks: `.buffer.read1()` が順に返すバイト列。

        Returns:
            なし
        """
        self.buffer = _BlockingBuffer(chunks)

    def isatty(self) -> bool:
        """常に非 TTY（パイプ接続）を表す False を返す。

        Returns:
            False
        """
        return False


class TestStdinIsAbsent:
    """`_stdin_is_absent`（読む対象があるかの判定）のテスト。

    ここが True になる経路だけが「payload なし = 素通り」であり、それ以外は
    実際に読んでみて判断する。select による readiness 判定は Windows の
    パイプで OSError になり「入力なし」に化けていたため撤去した（P1-004）。
    """

    def test_tty_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続（人手による直接起動）は payload なしとして扱う。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        assert hook_common._stdin_is_absent() is True

    def test_pipe_is_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 TTY のパイプは payload ありとして扱う。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload"))

        assert hook_common._stdin_is_absent() is False

    def test_stdin_none_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """detach された子プロセス等で sys.stdin が None の場合は payload なし。"""
        monkeypatch.setattr(hook_common.sys, "stdin", None)

        assert hook_common._stdin_is_absent() is True

    @pytest.mark.parametrize("error", [OSError("bad fd"), ValueError("closed"), AttributeError("x")])
    def test_isatty_failure_is_not_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """isatty() が例外化した場合は「無い」と決めつけず、読み取り側へ委ねる。

        ここで True を返すと、判定不能を「payload なし = 素通り」へ倒すことに
        なり、P1-004 と同じ fail-open を別経路で再生産する。
        """

        class _RaisingStdin:
            def isatty(self) -> bool:
                raise error

        monkeypatch.setattr(hook_common.sys, "stdin", _RaisingStdin())

        assert hook_common._stdin_is_absent() is False


class TestReadRawStdin:
    """read_raw_stdin のバイト単位制限・stdin ガードのテスト。"""

    def test_limits_by_bytes_not_chars_with_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer 付き stdin はバイト単位で読み取りを制限する。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("あ" * 10))

        result = hook_common.read_raw_stdin(max_bytes=10)

        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）
        assert result == "あああ�"

    def test_text_stdin_is_byte_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer を持たない stdin（io.StringIO 等）もバイト換算で切り捨てる。"""
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("あ" * 10))

        result = hook_common.read_raw_stdin(max_bytes=10)

        assert len(result.encode("utf-8")) <= 12  # 置換文字を含む 10 バイト相当
        assert result.startswith("あああ")

    def test_small_input_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """制限未満の入力はそのまま返る。"""
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("hello"))

        assert hook_common.read_raw_stdin() == "hello"

    def test_tty_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続では読まずに空文字列を返す。"""
        fake_stdin = _FakeStdin("payload", tty=True)
        monkeypatch.setattr(hook_common.sys, "stdin", fake_stdin)

        assert hook_common.read_raw_stdin() == ""

    def test_unavailable_stdin_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """読み取り不能でも空文字列を返す（非保護経路は fail-open のまま）。

        保護 hook 側は `read_raw_stdin_with_truncation` の
        `StdinUnavailableError` を捕捉して deny に倒す（ADR-0019）。両者を
        同じ関数にしないのは、launcher の `--bg` 中継や `mem context` には
        「読めなかった」ときに採れる別の行動が無いため。
        """
        monkeypatch.setattr(hook_common, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(hook_common.sys, "stdin", _BlockingStdin([]))

        assert hook_common.read_raw_stdin() == ""


class TestReadStdinBytesChunkedDeadline:
    """`_read_stdin_bytes` のチャンクループ・デッドライン挙動のテスト。

    NG-B2 回帰防止: `.buffer.read(max_bytes)` 1 回きりの旧実装は、書き手が
    `max_bytes` に満たないデータしか送らず接続も閉じない場合に無期限へ
    ブロックしていた。現行実装はブロッキング read をワーカースレッドへ隔離し、
    本スレッドは queue のタイムアウトで打ち切る（select は使わない）。
    """

    def test_multi_chunk_read_reassembles_full_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小さい STDIN_CHUNK_BYTES でも複数チャンクを結合して全データを返す。"""
        monkeypatch.setattr(hook_common, "STDIN_CHUNK_BYTES", 4)
        monkeypatch.setattr(hook_common.sys, "stdin", _QueueStdin([b"abcd", b"efgh", b"ij"]))

        assert hook_common._read_stdin_bytes(10) == b"abcdefghij"

    def test_chunk_request_size_respects_max_bytes_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """各 read1 の要求量が `min(STDIN_CHUNK_BYTES, max_bytes - collected)` を厳守する。

        `_QueueBuffer` は要求された `n` を実際に守ってスライスするため、
        呼び出し側が `min` を `max` と取り違えると 2 周目で `max_bytes` を
        超えるバイト列を集めてしまい、ここで検出できる。
        """
        monkeypatch.setattr(hook_common, "STDIN_CHUNK_BYTES", 4)
        monkeypatch.setattr(hook_common.sys, "stdin", _QueueStdin([b"ab", b"cdefghij"]))

        result = hook_common._read_stdin_bytes(5)

        assert result == b"abcde"

    def test_single_chunk_fast_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全データが 1 回のチャンク読みで収まる場合もそのまま返す。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _QueueStdin([b"hello"]))

        assert hook_common._read_stdin_bytes(10) == b"hello"

    def test_eof_before_max_bytes_returns_partial_data_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """書き手が max_bytes 未満で接続を閉じた（EOF）場合、警告なしで部分データを返す。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _QueueStdin([b"abc"]))

        result = hook_common._read_stdin_bytes(10)

        assert result == b"abc"
        assert capsys.readouterr().err == ""

    def test_stalled_writer_after_first_chunk_returns_partial_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1 チャンク受信後に書き手が止まったら、予算切れで部分データを返し警告する。"""
        monkeypatch.setattr(hook_common, "STDIN_READ_DEADLINE_SECONDS", 0.05)
        monkeypatch.setattr(hook_common.sys, "stdin", _BlockingStdin([b"abc"]))

        started = time.perf_counter()
        result = hook_common._read_stdin_bytes(10)
        elapsed = time.perf_counter() - started

        assert result == b"abc"
        assert elapsed < 5.0
        assert "上限時間に達した" in capsys.readouterr().err

    def test_first_byte_timeout_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """最初のバイトが届かないまま予算切れになったら例外を送出する。

        旧実装はここで空文字列を返しており、保護 hook が payload を 1 バイトも
        読まないまま許可側へ抜けていた（P1-004 の fail-open）。
        """
        monkeypatch.setattr(hook_common, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(hook_common.sys, "stdin", _BlockingStdin([]))

        with pytest.raises(hook_common.StdinUnavailableError):
            hook_common._read_stdin_bytes(10)

    @pytest.mark.parametrize("error", [OSError("bad fd"), ValueError("closed file")])
    def test_read1_failure_before_any_data_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """1 バイトも読めていない状態での read1 例外は「読めなかった」として送出する。

        Windows の通常パイプで select が OSError になっていた経路と同型で、
        ここを空文字列へ正規化すると allow と deny が同じ exit 0 になる。
        """

        class _RaisingBuffer:
            def read1(self, n: int = -1) -> bytes:
                raise error

        class _RaisingStdin:
            buffer = _RaisingBuffer()

            def isatty(self) -> bool:
                return False

        monkeypatch.setattr(hook_common.sys, "stdin", _RaisingStdin())

        with pytest.raises(hook_common.StdinUnavailableError):
            hook_common._read_stdin_bytes(10)

    def test_read1_failure_after_partial_data_returns_partial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """部分データを受け取った後の read1 例外は、その部分データで打ち切る。

        壊れた JSON として後段の `parse_json_object` が None を返し、保護 hook は
        そこで fail-closed に倒す（判定は 1 箇所に集約する）。
        """

        class _RaisingBuffer:
            def __init__(self, first: bytes) -> None:
                self._first = first
                self._served_first = False

            def read1(self, n: int = -1) -> bytes:
                if not self._served_first:
                    self._served_first = True
                    return self._first
                raise ValueError("I/O operation on closed file")

        class _RaisingStdin:
            def __init__(self, buffer: _RaisingBuffer) -> None:
                self.buffer = buffer

            def isatty(self) -> bool:
                return False

        monkeypatch.setattr(hook_common.sys, "stdin", _RaisingStdin(_RaisingBuffer(b"abc")))

        assert hook_common._read_stdin_bytes(10) == b"abc"

    def test_no_buffer_read_failure_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """buffer なし stdin の read() が失敗した場合も「読めなかった」として送出する。"""

        class _RaisingTextStdin:
            def read(self, n: int = -1) -> str:
                raise OSError("bad fd")

        monkeypatch.setattr(hook_common.sys, "stdin", _RaisingTextStdin())

        with pytest.raises(hook_common.StdinUnavailableError):
            hook_common._read_stdin_bytes(10)


class TestStdinChunkReader:
    """`_stdin_chunk_reader`（実 fd かバッファ API かの選択）のテスト。

    実 fd があるときに `BufferedReader.read1()` を使うと、書き手が止まった
    まま本スレッドが先へ進んだ場合に、インタプリタ終了時の stdin 後始末が
    ワーカーの握るロックを取れず `Fatal Python error: _enter_buffered_busy`
    で abort する（実測）。`os.read()` は Python レベルのロックを握らない。
    """

    def test_real_fd_is_read_without_buffer_lock(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """実 fd を持つ stdin は os.read 経由で読むこと。"""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"payload")
        os.close(write_fd)

        class _FdStdin:
            def __init__(self, fd: int) -> None:
                self.buffer = self
                self._fd = fd

            def fileno(self) -> int:
                return self._fd

            def isatty(self) -> bool:
                return False

            def read1(self, n: int = -1) -> bytes:
                raise AssertionError("実 fd がある場合は read1 を使わない")

        monkeypatch.setattr(hook_common.sys, "stdin", _FdStdin(read_fd))
        try:
            assert hook_common._read_stdin_bytes(100) == b"payload"
        finally:
            os.close(read_fd)

    def test_missing_fileno_falls_back_to_read1(self) -> None:
        """fileno を持たないオブジェクトは read1 で読むこと。"""
        buffer = _QueueBuffer([b"abc"])

        # bound method は同一性ではなく等価性で比較する。
        assert hook_common._stdin_chunk_reader(buffer) == buffer.read1

    @pytest.mark.parametrize("error", [OSError("no fd"), ValueError("closed")])
    def test_failing_fileno_falls_back_to_read1(self, error: Exception) -> None:
        """fileno() が例外化するオブジェクトも read1 で読むこと。"""

        class _NoFdBuffer:
            def fileno(self) -> int:
                raise error

            def read1(self, n: int = -1) -> bytes:
                return b""

        buffer = _NoFdBuffer()

        # bound method は同一性ではなく等価性で比較する。
        assert hook_common._stdin_chunk_reader(buffer) == buffer.read1


class TestStdinUnreadableMessage:
    """`stdin_unreadable_message` の文面契約。"""

    def test_message_names_the_hook_and_reason(self) -> None:
        """hook 名と理由の両方が deny 理由に載ること（診断可能性）。"""
        message = hook_common.stdin_unreadable_message("pre:block-no-verify", "boom")

        assert "pre:block-no-verify" in message
        assert "boom" in message
        assert "BLOCKED" in message


class TestResolveRepoRoot:
    """`resolve_repo_root`（A-06 共有ヘルパー）の解決・プロセス内キャッシュのテスト。

    `resolve_repo_root` は `functools.lru_cache` を持つため、各テストの
    前後で `cache_clear()` してテスト間の漏れ込みを防ぐ。
    """

    @pytest.fixture(autouse=True)
    def _clear_cache(self) -> None:
        hook_common.resolve_repo_root.cache_clear()
        yield
        hook_common.resolve_repo_root.cache_clear()

    def test_returns_none_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse が失敗（returncode != 0）した場合は None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 128, stdout="", stderr="fatal"),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_empty_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse の出力が空の場合は None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="", stderr=""),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_timeout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse がタイムアウトした場合は非ブロッキングで None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="git", timeout=5)),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_none_on_file_not_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git 実行ファイル自体が無い場合も非ブロッキングで None を返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("no git")),
        )
        assert hook_common.resolve_repo_root() is None

    def test_returns_path_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """git rev-parse が成功すればそのパスを返すこと。"""
        monkeypatch.setattr(
            hook_common.subprocess,
            "run",
            lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, stdout="/repo/root\n", stderr=""),
        )
        assert hook_common.resolve_repo_root() == Path("/repo/root")

    def test_caches_within_process_calls_git_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """同一プロセス内では git rev-parse を最大 1 回しか実行しない。"""
        call_count = {"n": 0}

        def _fake_run(*args, **kwargs):  # noqa: ANN002, ANN003
            call_count["n"] += 1
            return subprocess.CompletedProcess(args[0], 0, stdout="/repo/root\n", stderr="")

        monkeypatch.setattr(hook_common.subprocess, "run", _fake_run)

        first = hook_common.resolve_repo_root()
        second = hook_common.resolve_repo_root()

        assert first == second == Path("/repo/root")
        assert call_count["n"] == 1


class TestIsGitExecutableToken:
    """is_git_executable_token（H-04 共有正規化。block_no_verify /
    pre_bash_commit_quality が共に使う）のテスト。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("git", True),
            ("GIT", True),
            ("Git", True),
            ("/usr/bin/git", True),
            ("git.exe", True),
            ("GIT.EXE", True),
            (r"C:\Program Files\Git\bin\git.exe", True),
            ("gitk", False),
            (".git", False),
            ("git/", False),
        ],
    )
    def test_normalizes_case_path_and_exe_suffix(self, token: str, expected: bool) -> None:
        assert hook_common.is_git_executable_token(token) is expected


class TestNormalizeExecutableName:
    """normalize_executable_name（H-6 共有正規化）のテスト。

    `is_git_executable_token` / `bash_config_protection` の実行名照合 /
    `pre_bash_commit_quality` の mutation 語彙が同じ規則を使うことを固定する。
    片方だけ `.exe` を落とす・片方だけ大小を区別する状態が繰り返し出荷された。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("cp", "cp"),
            ("CP", "cp"),
            ("Rm", "rm"),
            ("TEE", "tee"),
            ("cp.exe", "cp"),
            ("RM.EXE", "rm"),
            ("/usr/bin/rm", "rm"),
            # クォートで守られた Windows 絶対パスは POSIX トークン化を `\` 付きで
            # 生き延びるため、`\` も区切りとして扱う必要がある。
            (r"C:\bin\rm.exe", "rm"),
            ("Remove-Item", "remove-item"),
            ("", ""),
        ],
    )
    def test_folds_case_separators_and_exe(self, token: str, expected: str) -> None:
        assert hook_common.normalize_executable_name(token) == expected


class TestNormalizeProtectedName:
    """normalize_protected_name（C-2 共有正規化）のテスト。

    実行名側と**同じ case 演算**を使うこと、および `.exe` は落とさないこと
    （保護対象は設定ファイルであり `ruff.toml.exe` を同一視する根拠が無い）を
    固定する。
    """

    @pytest.mark.parametrize(
        ("path", "expected"),
        [
            ("ruff.toml", "ruff.toml"),
            ("Ruff.toml", "ruff.toml"),
            ("RUFF.TOML", "ruff.toml"),
            ("sub/dir/PyProject.toml", "pyproject.toml"),
            (r"sub\dir\Package.json", "package.json"),
            (".GIT", ".git"),
            # 実行名と違い `.exe` は保持する。
            ("ruff.toml.exe", "ruff.toml.exe"),
        ],
    )
    def test_folds_case_and_separators(self, path: str, expected: str) -> None:
        assert hook_common.normalize_protected_name(path) == expected


class TestIsInplaceEditFlag:
    """is_inplace_edit_flag（H-5 共有述語）のテスト。

    `bash_config_protection`（保護対象への in-place 書き込み）と
    `pre_bash_commit_quality`（commit 前の作業ツリー変更）が同じ語彙を使うことを
    固定する。後者は `not arg.startswith("--")` で長形式を明示的に除外しており、
    `sed --in-place ... && git commit -am x` で両ガードが同時に不発だった。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            # 短形式（結合位置を問わない）。
            ("-i", True),
            ("-i.bak", True),
            ("-0pi", True),
            ("-ni", True),
            ("-pi", True),
            # GNU 長形式と、getopt が受け付ける非曖昧な短縮。
            ("--in-place", True),
            ("--in-place=.bak", True),
            ("--in-pl", True),
            ("--i", True),
            # 別の長形式は `in-place` のプレフィックスではない。
            ("--expression", False),
            ("--silent", False),
            ("--include", False),
            # 区切りの `--` は名前部分が空なので in-place ではない。
            ("--", False),
            ("--=x", False),
            # 単独の `-` と非オプション。
            ("-", False),
            ("-n", False),
            ("-E", False),
            ("s/a/b/", False),
            ("ruff.toml", False),
            ("", False),
        ],
    )
    def test_classifies_short_and_long_forms(self, token: str, expected: bool) -> None:
        assert hook_common.is_inplace_edit_flag(token) is expected


class TestResolveEffectiveTarget:
    """resolve_effective_target（H-02 共有 helper。config_protection /
    bash_config_protection が symlink 解決に使う）のテスト。
    """

    def test_resolves_relative_path_against_cwd(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "pyproject.toml"
        target.write_text("", encoding="utf-8")

        resolved = hook_common.resolve_effective_target("pyproject.toml")

        assert resolved == target.resolve()

    def test_follows_symlink_to_real_target(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        real = tmp_path / "pyproject.toml"
        real.write_text("", encoding="utf-8")
        alias = tmp_path / "alias-file"
        alias.symlink_to(real)

        assert hook_common.resolve_effective_target("alias-file") == real.resolve()

    def test_nonexistent_path_still_resolves(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """strict=False なので存在しないパスでも例外にせず解決した Path を返す。"""
        monkeypatch.chdir(tmp_path)

        resolved = hook_common.resolve_effective_target("does-not-exist.toml")

        assert resolved == tmp_path / "does-not-exist.toml"

    def test_returns_none_on_os_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """壊れた・循環した symlink 等で解決不能な場合は None を返す（クラッシュさせない）。"""

        def _boom(self, strict=False):  # noqa: ANN001, ANN002, ARG001
            raise OSError("elooped")

        monkeypatch.setattr(Path, "resolve", _boom)

        assert hook_common.resolve_effective_target("whatever") is None

    def test_returns_none_on_runtime_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def _boom(self, strict=False):  # noqa: ANN001, ANN002, ARG001
            raise RuntimeError("symlink loop")

        monkeypatch.setattr(Path, "resolve", _boom)

        assert hook_common.resolve_effective_target("whatever") is None


class TestReadRawStdinWithTruncation:
    """read_raw_stdin_with_truncation の切り捨て判定・stdin ガードのテスト。"""

    def test_no_truncation_when_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上限内の入力はそのまま、切り捨てフラグは False。"""
        monkeypatch.setattr(hook_common.sys, "stdin", io.StringIO("short"))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == "short"
        assert truncated is False

    def test_truncates_when_exceeding_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上限超過の入力は切り捨てたうえでフラグを立てる。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("a" * 20))

        text, truncated = hook_common.read_raw_stdin_with_truncation(max_bytes=10)

        assert text == "a" * 10
        assert truncated is True

    def test_tty_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続は payload なしとして空文字列を返す（例外にしない）。"""
        monkeypatch.setattr(hook_common.sys, "stdin", _FakeStdin("payload", tty=True))

        text, truncated = hook_common.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False

    def test_unreadable_stdin_raises_for_protection_hooks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """読み取り不能は例外にする（保護 hook が deny へ倒せるようにする）。"""
        monkeypatch.setattr(hook_common, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(hook_common.sys, "stdin", _BlockingStdin([]))

        with pytest.raises(hook_common.StdinUnavailableError):
            hook_common.read_raw_stdin_with_truncation()


class TestParseJsonObject:
    """parse_json_object の直接テスト。

    block_no_verify / config_protection は F-01/F-02 対応で raw が空の
    場合に parse_json_object を呼ばず早期 return するようになったため、
    空・空白のみ入力を渡す経路をここで直接固定する。
    """

    def test_empty_string_returns_none(self) -> None:
        assert hook_common.parse_json_object("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert hook_common.parse_json_object("   \n\t  ") is None

    def test_invalid_json_returns_none(self) -> None:
        assert hook_common.parse_json_object("{not-json") is None

    def test_valid_json_object_is_parsed(self) -> None:
        assert hook_common.parse_json_object('{"a": 1}') == {"a": 1}

    def test_valid_json_non_object_returns_none(self) -> None:
        assert hook_common.parse_json_object("[1, 2, 3]") is None


class TestDetachLogPath:
    """_detach_log_path のテスト。"""

    def test_returns_dated_path_under_ple4_logs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)

        path = hook_common._detach_log_path()

        assert path is not None
        assert path.parent == tmp_path / ".ple4" / "logs"
        assert path.name.startswith("bg-") and path.name.endswith(".log")

    def test_returns_none_when_log_dir_cannot_be_created(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ログディレクトリを作れない場合は None（呼び出し元は DEVNULL へ倒す）。"""
        monkeypatch.setattr(
            hook_common,
            "ensure_private_dir",
            lambda path: (_ for _ in ()).throw(OSError("disk full")),
        )

        assert hook_common._detach_log_path() is None


class TestReadTailBytes:
    """_read_tail_bytes のテスト。"""

    def test_reads_full_content_when_within_limit(self, tmp_path: Path) -> None:
        target = tmp_path / "small.log"
        target.write_text("hello\n", encoding="utf-8")
        assert hook_common._read_tail_bytes(target, 4096) == "hello\n"

    def test_reads_only_tail_when_exceeding_limit(self, tmp_path: Path) -> None:
        target = tmp_path / "big.log"
        target.write_bytes(b"A" * 100 + b"TAIL")
        assert hook_common._read_tail_bytes(target, 4) == "TAIL"

    def test_missing_file_returns_empty_string(self, tmp_path: Path) -> None:
        assert hook_common._read_tail_bytes(tmp_path / "missing.log", 4096) == ""


class TestRecentBgFailureNotice:
    """recent_bg_failure_notice のテスト（§6.2 対応）。"""

    def _log_dir(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        log_dir = tmp_path / ".ple4" / "logs"
        log_dir.mkdir(parents=True)
        return log_dir

    def test_no_log_dir_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        assert hook_common.recent_bg_failure_notice() == ""

    def test_empty_log_file_returns_empty_string(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """正常系（対象プロセスが何も出力しない）はログが空になり通知なし。"""
        log_dir = self._log_dir(monkeypatch, tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        (log_dir / f"bg-{today}.log").write_text("", encoding="utf-8")
        assert hook_common.recent_bg_failure_notice() == ""

    def test_todays_log_with_content_returns_one_line_notice(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        today = datetime.now().strftime("%Y-%m-%d")
        (log_dir / f"bg-{today}.log").write_text(
            "Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'\n",
            encoding="utf-8",
        )

        notice = hook_common.recent_bg_failure_notice()

        assert notice != ""
        assert notice.count("\n") == 0
        assert "ModuleNotFoundError" in notice
        assert f"bg-{today}.log" in notice

    def test_falls_back_to_yesterdays_log_when_today_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        (log_dir / f"bg-{yesterday}.log").write_text("boom\n", encoding="utf-8")

        notice = hook_common.recent_bg_failure_notice()

        assert "boom" in notice
        assert f"bg-{yesterday}.log" in notice

    def test_ignores_logs_older_than_two_days(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        log_dir = self._log_dir(monkeypatch, tmp_path)
        old = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
        (log_dir / f"bg-{old}.log").write_text("ancient failure\n", encoding="utf-8")

        assert hook_common.recent_bg_failure_notice() == ""


class TestGcDetachStdinOrphans:
    """`gc_detach_stdin_orphans`（detach 用 stdin 一時ファイルの回収）のテスト。

    POSIX では起動直後に unlink されるため孤児は生じないが、Windows は開いて
    いるファイルを削除できず SessionEnd ごとに 1 個ずつ積み上がる（W4）。
    回収は生成側と同じ `get_ple4_dir()` 基準で行う。
    """

    def test_stale_orphan_is_removed(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """猶予を過ぎた `*.stdin` 孤児を回収すること。"""
        monkeypatch.setattr(hook_common, "get_ple4_dir", lambda: tmp_path)
        stale = tmp_path / "tmpstale.stdin"
        stale.write_text("payload", encoding="utf-8")
        old = time.time() - hook_common._DETACH_STDIN_GRACE_SECONDS - 60
        os.utime(stale, (old, old))

        hook_common.gc_detach_stdin_orphans()

        assert not stale.exists()

    def test_fresh_orphan_and_other_files_survive(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """実行中の detach が握るファイルと、拡張子が違うファイルは残すこと。"""
        monkeypatch.setattr(hook_common, "get_ple4_dir", lambda: tmp_path)
        fresh = tmp_path / "tmpfresh.stdin"
        other = tmp_path / "mem.db"
        fresh.write_text("payload", encoding="utf-8")
        other.write_text("db", encoding="utf-8")
        old = time.time() - hook_common._DETACH_STDIN_GRACE_SECONDS - 60
        os.utime(other, (old, old))

        hook_common.gc_detach_stdin_orphans()

        assert fresh.exists()
        assert other.exists()

    def test_missing_directory_is_noop(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ディレクトリが無くても例外を送出しないこと。"""
        monkeypatch.setattr(hook_common, "get_ple4_dir", lambda: tmp_path / "missing")

        hook_common.gc_detach_stdin_orphans()

    def test_stat_failure_is_ignored(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """個々のエントリの stat 失敗は無視して走査を続けること。"""
        monkeypatch.setattr(hook_common, "get_ple4_dir", lambda: tmp_path)
        entry = tmp_path / "tmpfail.stdin"
        entry.write_text("payload", encoding="utf-8")
        real_stat = Path.stat

        def _boom(self: Path, *args: object, **kwargs: object) -> object:
            if self == entry:
                raise OSError("boom")
            return real_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", _boom)

        hook_common.gc_detach_stdin_orphans()

        assert entry.exists()


class TestDetachedSpawnKwargs:
    """`detached_spawn_kwargs`（プロセスグループ分離）のプラットフォーム契約。

    `start_new_session=True` は Windows で `ValueError` になり、SessionEnd の
    handoff hook がそこで落ちていた（P1-013）。プラットフォーム名ではなく
    `os.setsid` の有無という capability で分岐することを固定する。
    """

    def test_posix_uses_new_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`os.setsid` がある環境では start_new_session を使うこと。"""
        monkeypatch.setattr(hook_common.os, "setsid", lambda: None, raising=False)

        assert hook_common.detached_spawn_kwargs() == {"start_new_session": True}

    def test_windows_uses_creation_flags(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`os.setsid` が無い環境では creationflags でプロセスグループを分けること。"""
        monkeypatch.delattr(hook_common.os, "setsid", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
        monkeypatch.setattr(hook_common.subprocess, "DETACHED_PROCESS", 0x8, raising=False)

        assert hook_common.detached_spawn_kwargs() == {"creationflags": 0x208}


class TestDetachProcess:
    """detach_process の一時ファイル経由 stdin 引き渡し・エラー処理のテスト。

    呼び出し元は launcher.py の `--bg` 実行 1 箇所のみ（`_run_background`）。
    """

    def test_launches_detached_process_and_cleans_up_tmp_file(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        captured = {}
        written_stdin_content = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["cmd"] = cmd
                captured["env"] = env
                captured["start_new_session"] = start_new_session
                stdin.seek(0)
                written_stdin_content["text"] = stdin.read()

        monkeypatch.setattr(hook_common.subprocess, "Popen", _FakePopen)

        result = detach_process(["python3", "-m", "ple4.mem.cli", "observe"], "raw-payload", env={"X": "1"})

        assert result is True
        assert captured["cmd"][:3] == [hook_common.sys.executable, "-c", hook_common._WATCHDOG_SCRIPT]
        assert captured["cmd"][3:5] == [
            str(hook_common.DETACH_TIMEOUT_SECONDS),
            str(hook_common._DETACH_KILL_AFTER_SECONDS),
        ]
        assert captured["cmd"][5:] == ["python3", "-m", "ple4.mem.cli", "observe"]
        assert captured["env"]["X"] == "1"
        # 呼び出し元の env はそのまま維持しつつ、対象プロセスの stdout/stderr を
        # DEVNULL の代わりに追記させるログパスが差し込まれること（F-18 対応）。
        log_path = Path(captured["env"]["PLE4_BG_LOG_PATH"])
        assert log_path.parent == tmp_path / ".ple4" / "logs"
        assert log_path.name.startswith("bg-") and log_path.name.endswith(".log")
        assert captured["start_new_session"] is True
        assert written_stdin_content["text"] == "raw-payload"
        # 起動直後に unlink 済みで、ディレクトリにファイルが残らないこと。
        assert list((tmp_path / ".ple4").glob("*.stdin")) == []

    def test_env_none_still_gets_log_path_injected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """env=None（親環境継承）でもログパスは差し込まれること。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["env"] = env
                stdin.seek(0)

        monkeypatch.setattr(hook_common.subprocess, "Popen", _FakePopen)

        assert detach_process(["true"], "raw") is True
        assert "PLE4_BG_LOG_PATH" in captured["env"]

    def test_log_dir_creation_failure_falls_back_to_devnull(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ログディレクトリを作れなくても detach 自体は継続する（best-effort）。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        captured = {}

        class _FakePopen:
            def __init__(self, cmd, *, stdin=None, stdout=None, stderr=None, env=None, start_new_session=None):  # noqa: ANN001
                captured["env"] = env
                stdin.seek(0)

        monkeypatch.setattr(hook_common.subprocess, "Popen", _FakePopen)
        monkeypatch.setattr(hook_common, "_detach_log_path", lambda: None)

        assert detach_process(["true"], "raw", env={"X": "1"}) is True
        assert captured["env"] == {"X": "1"}
        assert "PLE4_BG_LOG_PATH" not in captured["env"]

    def test_tempfile_creation_failure_returns_false(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)

        def fail_named_temp_file(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("disk full")

        monkeypatch.setattr(hook_common.tempfile, "NamedTemporaryFile", fail_named_temp_file)

        assert detach_process(["true"], "raw") is False

    def test_popen_failure_returns_false_and_cleans_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)

        def fail_popen(*args, **kwargs):  # noqa: ANN002, ANN003
            raise OSError("spawn failed")

        monkeypatch.setattr(hook_common.subprocess, "Popen", fail_popen)

        assert detach_process(["true"], "raw") is False
        assert list((tmp_path / ".ple4").glob("*.stdin")) == []

    def test_unlink_failure_during_cleanup_is_swallowed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """一時ファイルの unlink 失敗（既に削除済み等）でも起動成功を維持する。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)

        def fail_unlink(path):  # noqa: ANN001
            raise OSError("already removed")

        monkeypatch.setattr(hook_common.os, "unlink", fail_unlink)

        assert detach_process(["true"], "raw") is True

    def test_creates_ple4_dir_as_0700_under_umask_022(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """umask 022 でも ~/.ple4 を 0700 で作る。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)
        old_umask = os.umask(0o022)
        try:
            assert detach_process(["true"], "raw") is True
            mode = stat.S_IMODE((tmp_path / ".ple4").stat().st_mode)
            assert mode == 0o700
        finally:
            os.umask(old_umask)

    def test_tightens_existing_0755_ple4_dir(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """既存 0755 の ~/.ple4 を 0700 に締め直す。"""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PLE4_HOME", raising=False)
        monkeypatch.setattr(hook_common.subprocess, "Popen", lambda *a, **k: None)
        ple4 = tmp_path / ".ple4"
        ple4.mkdir(mode=0o755)
        ple4.chmod(0o755)
        assert detach_process(["true"], "raw") is True
        assert stat.S_IMODE(ple4.stat().st_mode) == 0o700


def _pid_alive(pid: int) -> bool:
    """指定 PID のプロセスが生存しているかを判定する。

    Args:
        pid: 判定対象のプロセス ID。

    Returns:
        生存していれば True。

    Raises:
        例外は発生しません。
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _wait_until_dead(pid: int, deadline_seconds: float = 10.0) -> bool:
    """PID が消えるまでポーリングし、消えたかどうかを返す。

    Args:
        pid: 判定対象のプロセス ID。
        deadline_seconds: 待機する最大秒数。

    Returns:
        期限内にプロセスが消えたら True。

    Raises:
        例外は発生しません。
    """
    limit = time.monotonic() + deadline_seconds
    while time.monotonic() < limit:
        if not _pid_alive(pid):
            return True
        time.sleep(0.05)
    return False


class TestWatchdogKillsProcessGroup:
    """_WATCHDOG_SCRIPT が孫プロセスまで実プロセスで確実に殺すことのテスト。

    Popen.terminate()/kill() は直接の子 1 プロセスにしか届かず、子が起動した
    孫（desktop_notify の osascript / PowerShell 等）が残留する退行があった
    ため、argv 一致アサートではなく実際にプロセスを起動して wall-clock で
    検証する。
    """

    def _spawn_watchdog(
        self,
        tmp_path: Path,
        *,
        timeout: float,
        kill_after: float,
        ignore_sigterm: bool,
    ) -> tuple[subprocess.Popen, Path, int]:
        """watchdog → 子 → 孫の 3 段プロセスを起動し、孫の PID を返す。

        孫は `grandchild_sleep_seconds` 秒後にマーカーファイルを書くため、
        マーカーが存在しないことが「孫が仕事を完了する前に殺された」証跡になる。

        Args:
            tmp_path: マーカー / PID ファイルを置く一時ディレクトリ。
            timeout: watchdog が子へ SIGTERM を送るまでの秒数。
            kill_after: SIGTERM 後 SIGKILL へ昇格するまでの猶予秒数。
            ignore_sigterm: True なら子と孫が SIGTERM を無視する。

        Returns:
            (watchdog の Popen, マーカーパス, 孫の PID) のタプル。

        Raises:
            AssertionError: 孫の PID ファイルが期限内に作られない場合。
        """
        marker = tmp_path / "grandchild-marker"
        pid_file = tmp_path / "grandchild-pid"
        guard = "import signal;signal.signal(signal.SIGTERM, signal.SIG_IGN);" if ignore_sigterm else ""
        grandchild = f"{guard}import time;time.sleep(5);open({str(marker)!r},'w').write('alive')"
        child = (
            f"{guard}import subprocess,sys,time;"
            f"p=subprocess.Popen([sys.executable,'-c',{grandchild!r}]);"
            f"open({str(pid_file)!r},'w').write(str(p.pid));"
            "time.sleep(60)"
        )
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                hook_common._WATCHDOG_SCRIPT,
                str(timeout),
                str(kill_after),
                sys.executable,
                "-c",
                child,
            ]
        )
        limit = time.monotonic() + 10.0
        while time.monotonic() < limit and not pid_file.exists():
            time.sleep(0.02)
        assert pid_file.exists(), "孫プロセスが起動しなかった"
        return proc, marker, int(pid_file.read_text())

    def test_timeout_kills_grandchild(self, tmp_path: Path) -> None:
        """timeout 到達時、子だけでなく孫もプロセスグループごと殺される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=0.3, kill_after=0.3, ignore_sigterm=False
        )
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "孫プロセスが生存し続けた"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"

    def test_sigterm_ignoring_grandchild_is_escalated_to_sigkill(self, tmp_path: Path) -> None:
        """SIGTERM を無視する孫も kill_after 経過後 SIGKILL で回収される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=0.3, kill_after=0.3, ignore_sigterm=True
        )
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "SIGTERM を無視する孫が生存し続けた"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"

    def test_watchdog_sigterm_cascades_to_grandchild(self, tmp_path: Path) -> None:
        """watchdog 自身が SIGTERM を受けたとき、孫まで cascade して殺される。"""
        proc, marker, grandchild_pid = self._spawn_watchdog(
            tmp_path, timeout=60, kill_after=0.3, ignore_sigterm=False
        )
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=30)

        assert _wait_until_dead(grandchild_pid), "watchdog 終了後に孫が残留した"
        assert not marker.exists(), "孫プロセスが仕事を完了してしまった"


class TestHeredocNormalization:
    """heredoc のデータ本文をコマンド解析から外す前段パスのテスト。

    heredoc 本文はどのシェルでもコマンドの語彙に入らないが、shlex は本文行も
    通常のトークンとして返す。そのため散文が実行命令として読まれ、実測で
    3 つの保護フックが `cat > note.md <<'EOF'` の本文に書いた語だけで exit 2
    になった。ただし `bash <<'EOF'` では本文が実際に実行されるため、剥がして
    よいのは本文がデータだと静的に確定できる形だけに限る。
    """

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("sh", True),
            ("bash", True),
            ("/bin/zsh", True),
            ("dash", True),
            ("shell", False),
            ("bashrc", False),
            ("git", False),
            ("", False),
        ],
    )
    def test_is_shell_wrapper_token(self, token: str, expected: bool) -> None:
        """basename 一致であって前方一致ではないこと。"""
        assert hook_common.is_shell_wrapper_token(token) is expected

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("cat <<EOF", [("EOF", False)]),
            ("cat << EOF", [("EOF", False)]),
            ("cat <<-EOF", [("EOF", True)]),
            ("cat <<'E O F'", [("E O F", False)]),
            ('cat <<"EOF"', [("EOF", False)]),
            ("cat <<\\EOF", [("EOF", False)]),
            ("cat <<''", [("", False)]),
            ("grep x <<< 'y'", []),
            ("grep x <<<EOF", []),
            ("echo hi", []),
            ("cat <<A <<B", [("A", False), ("B", False)]),
            # 「幻の演算子」: heredoc を開始しない `<<` を演算子として採らない。
            # 採ると後続の**実行される**行が本文として捨てられ、保護フックが
            # 素通りする（実測。C-1）。
            ("# <<EOF", []),
            ("  # <<EOF", []),
            ("cat x # <<EOF", []),
            ('echo "<<EOF"', []),
            ("echo '<<EOF'", []),
            (r"echo \<<EOF", []),
            # 陰性対照: 演算子として採るべき形を巻き込んで消していないこと。
            ("echo a#b <<EOF", [("EOF", False)]),
            ("cat <<EOF # note", [("EOF", False)]),
            ("cat <<EOF > out.md", [("EOF", False)]),
            ("echo '#' && cat <<EOF", [("EOF", False)]),
        ],
    )
    def test_heredoc_delimiters(self, line: str, expected: list) -> None:
        """区切り語の抽出。`<<<`（herestring）とコメント内・クォート内は拾わない。"""
        assert hook_common._heredoc_delimiters(line, None) == (expected, None)

    @pytest.mark.parametrize(
        ("label", "line", "quote", "expected"),
        [
            (
                "開いたダブルクォートの内側は演算子にしない",
                "still quoted <<EOF",
                '"',
                ([], '"'),
            ),
            (
                "行内でクォートが閉じれば以降は演算子",
                'end" && cat <<EOF',
                '"',
                ([("EOF", False)], None),
            ),
            (
                "開いたシングルクォート内の \\ は literal（エスケープしない）",
                r"a\' <<EOF",
                "'",
                ([("EOF", False)], None),
            ),
            (
                "開いたダブルクォート内の \\ は次の 1 文字を消費する",
                r'a\" <<EOF',
                '"',
                ([], '"'),
            ),
            (
                "閉じないクォートは次行へ持ち越す",
                'echo "open',
                None,
                ([], '"'),
            ),
            (
                "開いたクォート内の # はコメントにならない",
                "a # b\" && cat <<EOF",
                '"',
                ([("EOF", False)], None),
            ),
        ],
    )
    def test_heredoc_delimiters_carries_quote_state(
        self, label: str, line: str, quote: str | None, expected: tuple
    ) -> None:
        """クォート状態は行をまたいで持ち越される（シェルのクォートは改行を含む）。"""
        assert hook_common._heredoc_delimiters(line, quote) == expected, label

    @pytest.mark.parametrize(
        ("label", "line", "expected"),
        [
            ("演算子にならない < は候補のまま照合で落ちる", "a < b", ([2], [])),
            ("herestring は候補 3 つすべてが照合で落ちる", "x <<<y", ([2, 3, 4], [])),
            ("同一演算子の 2 文字目は照合済み範囲として飛ばす", "cat <<A", ([4, 5], [("A", False)])),
            ("行末の \\ は次行へ持ち越さない", "cat <<A \\", ([4, 5], [("A", False)])),
        ],
    )
    def test_operator_start_offsets(self, label: str, line: str, expected: tuple) -> None:
        """候補位置の抽出と、そこからの非重複照合。

        候補が挙がっても `_HEREDOC_OPERATOR_RE` が一致しなければ区切り語にならない。
        1 つの演算子の 2 文字目（`<<A` の 2 つ目の `<`）は直前の照合範囲内なので
        `finditer` と同じく飛ばす。
        """
        offsets, quote = hook_common._operator_start_offsets(line, None)

        assert (offsets, quote) == (expected[0], None), label
        assert hook_common._heredoc_delimiters(line, None) == (expected[1], None), label

    @pytest.mark.parametrize(
        ("line", "expected"),
        [
            ("cat > note.md <<'EOF'", False),
            ("tee note.md <<'EOF'", False),
            ("bash <<'EOF'", True),
            ("sudo bash <<'EOF'", True),
            ("cat <<'EOF' | bash", True),
            ("cat <<'EOF' |", True),
            ("cat <<'EOF' &", True),
            ("cat <<'EOF' \\", True),
            ("echo bash <<'EOF'", True),
        ],
    )
    def test_line_keeps_heredoc_bodies(self, line: str, expected: bool) -> None:
        """本文を残す/剥がすの判定。疑わしい形はすべて残す側へ倒す。"""
        assert hook_common._line_keeps_heredoc_bodies(line) is expected

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("heredoc 無しは素通り", "echo 'git commit --no-verify'", "echo 'git commit --no-verify'"),
            ("herestring は素通り", "grep x <<< 'git commit'", "grep x <<< 'git commit'"),
            (
                "データ本文は剥がす",
                "cat > note.md <<'EOF'\ngit commit --no-verify\nEOF",
                "cat > note.md <<'EOF'\nEOF",
            ),
            (
                "未クォート区切りでも剥がす",
                "cat <<EOF > note.md\nprintf x > pyproject.toml\nEOF",
                "cat <<EOF > note.md\nEOF",
            ),
            (
                "シェル起動なら残す",
                "bash <<'EOF'\ngit commit --no-verify\nEOF",
                "bash <<'EOF'\ngit commit --no-verify\nEOF",
            ),
            (
                "パイプ先がシェルなら残す",
                "cat <<'EOF' | bash\ngit commit --no-verify\nEOF",
                "cat <<'EOF' | bash\ngit commit --no-verify\nEOF",
            ),
            (
                "継続演算子で終わるなら残す",
                "cat <<'EOF' |\ngit commit --no-verify\nEOF",
                "cat <<'EOF' |\ngit commit --no-verify\nEOF",
            ),
            (
                "未終端なら残す",
                "cat > note.md <<'EOF'\ngit commit --no-verify",
                "cat > note.md <<'EOF'\ngit commit --no-verify",
            ),
            (
                "終端後の後続コマンドは残る",
                "cat > n.md <<'EOF'\nprose\nEOF\ngit commit -m x",
                "cat > n.md <<'EOF'\nEOF\ngit commit -m x",
            ),
            ("同一行の二重 heredoc", "cat <<A <<B\nb1\nA\nb2\nB", "cat <<A <<B\nA\nB"),
            (
                "本文中の <<'INNER' は再検出しない",
                "cat > n.md <<'OUTER'\ncat <<'INNER'\nx\nINNER\nOUTER",
                "cat > n.md <<'OUTER'\nOUTER",
            ),
            (
                "<<- はタブを剥がして終端照合",
                "cat > n.md <<-\tEOF\n\tgit commit --no-verify\n\tEOF",
                "cat > n.md <<-\tEOF\n\t\tEOF".replace("\t\t", "\t"),
            ),
            # ここから下は ADR-0017 が「未検証」「単純化されやすい」と名指しした境界。
            # CRLF: 区切り語はクォートの内側から取るため CR を含まず（`EOF`）、行側は
            # `EOF\r` なので一致しない。bash は逆に区切り語自体が CR を吸うため
            # （`<<'EOF'\r` の語は `EOF\r`）実際には終端する。挙動は一致しないが、
            # 剥がさない側は検出側であり ADR-0017 の決定 3（未終端なら剥がさない）に沿う。
            (
                "CRLF は区切り語と終端行が一致せず未終端側へ倒れる",
                "cat > note.md <<'EOF'\r\ngit commit --no-verify\r\nEOF\r\n",
                "cat > note.md <<'EOF'\r\ngit commit --no-verify\r\nEOF\r\n",
            ),
            (
                "後続に空白のある終端行は終端せず本文として扱う",
                "cat > n.md <<'EOF'\nprose\nEOF \nEOF",
                "cat > n.md <<'EOF'\nEOF",
            ),
            (
                "空白付きの行しか無ければ未終端として剥がさない",
                "cat > n.md <<'EOF'\ngit commit --no-verify\nEOF ",
                "cat > n.md <<'EOF'\ngit commit --no-verify\nEOF ",
            ),
            (
                "継続演算子で改行し次行が bash でも残す",
                "cat <<'EOF' |\nbash\ngit commit --no-verify\nEOF",
                "cat <<'EOF' |\nbash\ngit commit --no-verify\nEOF",
            ),
            # C-1「幻の演算子」。heredoc が開始しない `<<` を演算子として採ると、
            # 実行される後続行が本文として消え、保護フック 3 種が同時に素通りする。
            (
                "コメント内の << では本文を剥がさない",
                "# <<EOF\ngit commit --no-verify -m x\nEOF",
                "# <<EOF\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "ダブルクォート内の << では本文を剥がさない",
                'echo "<<EOF"\ngit commit --no-verify -m x\nEOF',
                'echo "<<EOF"\ngit commit --no-verify -m x\nEOF',
            ),
            (
                "シングルクォート内の << では本文を剥がさない",
                "echo '<<EOF'\ngit commit --no-verify -m x\nEOF",
                "echo '<<EOF'\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "エスケープされた << では本文を剥がさない",
                "echo \\<<EOF\ngit commit --no-verify -m x\nEOF",
                "echo \\<<EOF\ngit commit --no-verify -m x\nEOF",
            ),
            (
                "行をまたいで開いたクォート内の << も演算子にしない",
                'echo "open\n<<EOF\ngit commit --no-verify\nEOF',
                'echo "open\n<<EOF\ngit commit --no-verify\nEOF',
            ),
            # 陰性対照: 状態追跡が本物の heredoc の本文剥がしを壊していないこと。
            (
                "演算子行の行末コメントは本文剥がしを妨げない",
                "cat > n.md <<'EOF' # note\ngit commit --no-verify\nEOF",
                "cat > n.md <<'EOF' # note\nEOF",
            ),
            (
                "本文の # とアポストロフィは状態を持ち越さない",
                "cat > n.md <<'EOF'\ndon't # note\nEOF\ngit commit --no-verify",
                "cat > n.md <<'EOF'\nEOF\ngit commit --no-verify",
            ),
            (
                "終端行が引用符を含んでも状態を持ち越さない",
                "cat > n.md <<\"E'F\"\nprose\nE'F\ngit commit --no-verify",
                "cat > n.md <<\"E'F\"\nE'F\ngit commit --no-verify",
            ),
        ],
    )
    def test_strip_data_heredoc_bodies(self, label: str, command: str, expected: str) -> None:
        """データ本文だけを落とし、実行されうる形はそのまま残す。

        末尾の境界行は ADR-0017 の「否定的」「リスク」節が名指しした未検証の境界を
        固定する。CRLF 行は区切り語と一致せず未終端側へ、空白付きの `EOF ` は終端と
        認めず本文として扱い（後続に厳密一致の行があればそこで終端し、無ければ
        未終端）、継続演算子で終わる行は次行が `bash` でも本文を剥がさない。
        """
        assert hook_common.strip_data_heredoc_bodies(command) == expected, label

    @pytest.mark.parametrize(
        "command",
        [
            "cat > note.md <<'EOF'\nprose\nEOF",
            "bash <<'EOF'\ngit commit\nEOF",
            "cat > note.md <<'EOF'\nunterminated",
            "cat > note.md <<'EOF'\nprose\nEOF \nEOF",
            "cat > note.md <<'EOF'\r\nprose\r\nEOF\r\n",
            "echo hi",
            "# <<EOF\ngit commit --no-verify\nEOF",
            'echo "<<EOF"\ngit commit --no-verify\nEOF',
            "cat > n.md <<'EOF'\ndon't # note\nEOF\ngit commit --no-verify",
        ],
    )
    def test_strip_is_idempotent(self, command: str) -> None:
        """終端行を残す設計により再適用しても結果が変わらない。

        フックごとに適用点が異なるため、二重適用が起きても安全であることを固定する。
        """
        once = hook_common.strip_data_heredoc_bodies(command)

        assert hook_common.strip_data_heredoc_bodies(once) == once


class TestUnquotedNewlineNormalization:
    """クォート外の改行をコマンド区切りとして扱う正規化のテスト。

    改行はシェルにとって `;` と等価だが shlex は whitespace として消費するため
    区切りトークンを出さない。実測では複数行コマンドが 1 セグメントへ融合し、
    `printf x > s.py` 改行 `git add s.py` 改行 `git commit -m x` が exit 0 で通った
    （`;` 区切りの同内容は exit 2）。
    """

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            ("改行なしは素通り", "git commit -m x", "git commit -m x"),
            ("クォート外の改行は ; になる", "git add .\ngit commit -m x", "git add .;git commit -m x"),
            ("シングルクォート内の改行は保つ", "echo 'a\nb'", "echo 'a\nb'"),
            ("ダブルクォート内の改行は保つ", 'echo "a\nb"', 'echo "a\nb"'),
            ("クォートを閉じた後の改行は ; になる", "echo 'a'\ngit commit", "echo 'a';git commit"),
            ("バックスラッシュ継続は区切りにしない", "git add \\\n.", "git add \\\n."),
            ("シングルクォート内のバックスラッシュは継続にしない", "echo 'a\\'\nb", "echo 'a\\';b"),
            ("閉じないシングルクォート内の改行は保つ", "echo 'unterminated\nrm -rf /", "echo 'unterminated\nrm -rf /"),
            ('閉じないダブルクォート内の改行は保つ', 'echo "unterminated\nrm -rf /', 'echo "unterminated\nrm -rf /'),
        ],
    )
    def test_replace_unquoted_newlines(self, label: str, command: str, expected: str) -> None:
        """クォート外の改行だけを区切りへ置き換える。"""
        assert hook_common._replace_unquoted_newlines(command) == expected, label

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            (
                "シングルクォートが閉じない",
                "echo 'unterminated\nrm -rf /",
                (["echo", "'unterminated", "rm", "-rf", "/"], False),
            ),
            (
                "ダブルクォートが閉じない",
                'echo "unterminated\nrm -rf /',
                (["echo", '"unterminated', "rm", "-rf", "/"], False),
            ),
        ],
    )
    def test_tokenize_with_status_unterminated_quote(self, label: str, command: str, expected: tuple) -> None:
        """クォートが閉じない入力では改行を区切りにしないが、後続行の語は走査対象に残る。

        `_replace_unquoted_newlines` は開いたクォートの内側とみなして `;` を挿さない。
        代わりに `shlex` が `ValueError` になり空白分割へフォールバックするため、改行の
        後ろにある語もトークンとして現れる（解析できなかった印として第 2 要素は False。
        呼び出し側はこれを見て生文字列の正規表現へ倒す＝fail-closed 側になる）。
        """
        assert hook_common.tokenize_with_status(command) == expected, label

    def test_multiline_command_splits_into_segments(self) -> None:
        """複数行コマンドが行ごとのセグメントへ分かれること。"""
        tokens = hook_common.tokenize("printf x > s.py\ngit add s.py\ngit commit -m m")

        assert hook_common.split_segments(tokens) == [
            ["printf", "x", ">", "s.py"],
            ["git", "add", "s.py"],
            ["git", "commit", "-m", "m"],
        ]

    @pytest.mark.parametrize(
        ("label", "command", "expected"),
        [
            (
                "エスケープした # は語の一部",
                r"echo \# && git commit --no-verify",
                ["echo", "#", "&&", "git", "commit", "--no-verify"],
            ),
            (
                "シングルクォート内の \\ は literal",
                r"echo 'a\b' # 注釈",
                ["echo", r"a\b"],
            ),
            (
                "ダブルクォート内の # はコメントにならない",
                'echo "a # b" && git commit --no-verify',
                ["echo", "a # b", "&&", "git", "commit", "--no-verify"],
            ),
        ],
    )
    def test_line_comment_stripping_respects_quotes_and_escapes(
        self, label: str, command: str, expected: list[str]
    ) -> None:
        """`#` の読み捨ては行末までで、クォート内・エスケープ後には及ばないこと。

        `shlex` のコメント処理に任せていた頃は `#` 以降が入力**末尾**まで捨てられ、
        2 行目の `git commit --no-verify` が丸ごと未検査になっていた（実測）。
        """
        assert hook_common.tokenize(command) == expected, label
