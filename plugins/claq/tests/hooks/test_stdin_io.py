"""stdin_io の stdin 読み取りガード（TTY/タイムアウト/バイト上限）と JSON 化のテスト。"""

from __future__ import annotations

import io
import os
import threading
import time

import pytest

from claq.hooks import stdin_io


class _FakeBuffer:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read1(self, n: int = -1) -> bytes:
        return self._data[:n] if n >= 0 else self._data


class _FakeStdin:
    """isatty() を備えた stdin の代替オブジェクト。

    ``isatty_calls`` に isatty() の呼び出し回数を記録する。pytest 既定の stdin は非 TTY なので、
    差し替えが外れても「非 TTY」と答える判定は結果が変わらない。呼ばれた回数で差し替えの到達を固定する。
    """

    def __init__(self, text: str, *, tty: bool = False) -> None:
        self._tty = tty
        self.buffer = _FakeBuffer(text.encode("utf-8"))
        self.read_called = False
        self.isatty_calls = 0

    def isatty(self) -> bool:
        self.isatty_calls += 1
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
        self.requested: list[int] = []

    def read1(self, n: int = -1) -> bytes:
        """キューの先頭チャンクから最大 `n` バイトを返す。尽きていれば EOF（b""）。

        呼び出しごとの要求量 `n` を ``requested`` に記録する（チャンクサイズ定数が使われたかの検査用）。

        `n` バイト以上あれば先頭 `n` バイトだけを返し、残りは次回呼び出し用に
        キューの先頭へ戻す（実 `read1()` が要求量を超えて返さない挙動を模す）。

        Args:
            n: 呼び出し側が要求する最大バイト数。負値なら無制限。

        Returns:
            キューの次のバイト列（最大 `n` バイト）、または尽きていれば b""。
        """
        self.requested.append(n)
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
    パイプで OSError になり「入力なし」に化けるため使わない。
    """

    def test_tty_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続（人手による直接起動）は payload なしとして扱う。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin("payload", tty=True))

        assert stdin_io._stdin_is_absent() is True

    def test_pipe_is_present(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """非 TTY のパイプは payload ありとして扱う。"""
        fake_stdin = _FakeStdin("payload")
        monkeypatch.setattr(stdin_io.sys, "stdin", fake_stdin)

        assert stdin_io._stdin_is_absent() is False
        assert fake_stdin.isatty_calls == 1

    def test_stdin_none_is_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """detach された子プロセス等で sys.stdin が None の場合は payload なし。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", None)

        assert stdin_io._stdin_is_absent() is True

    @pytest.mark.parametrize("error", [OSError("bad fd"), ValueError("closed"), AttributeError("x")])
    def test_isatty_failure_is_not_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """isatty() が例外化した場合は「無い」と決めつけず、読み取り側へ委ねる。

        ここで True を返すと、判定不能を「payload なし = 素通り」へ倒すことに
        なり、Windows のパイプで起きる fail-open を別経路で再生産する。
        """

        isatty_calls: list[None] = []

        class _RaisingStdin:
            """isatty() の呼び出しを記録して例外化する stdin。"""

            def isatty(self) -> bool:
                isatty_calls.append(None)
                raise error

        monkeypatch.setattr(stdin_io.sys, "stdin", _RaisingStdin())

        assert stdin_io._stdin_is_absent() is False
        # pytest 既定の stdin も非 TTY で False を返すため、差し替えが効いたことは呼び出しで固定する。
        assert isatty_calls == [None]


class TestReadRawStdin:
    """read_raw_stdin のバイト単位制限・stdin ガードのテスト。"""

    def test_limits_by_bytes_not_chars_with_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer 付き stdin はバイト単位で読み取りを制限する。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin("あ" * 10))

        result = stdin_io.read_raw_stdin(max_bytes=10)

        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）
        assert result == "あああ�"

    def test_text_stdin_is_byte_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """buffer を持たない stdin（io.StringIO 等）もバイト換算で切り捨てる。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", io.StringIO("あ" * 10))

        result = stdin_io.read_raw_stdin(max_bytes=10)

        assert len(result.encode("utf-8")) <= 12  # 置換文字を含む 10 バイト相当
        assert result.startswith("あああ")

    def test_small_input_passes_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """制限未満の入力はそのまま返る。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", io.StringIO("hello"))

        assert stdin_io.read_raw_stdin() == "hello"

    def test_tty_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続では読まずに空文字列を返す。"""
        fake_stdin = _FakeStdin("payload", tty=True)
        monkeypatch.setattr(stdin_io.sys, "stdin", fake_stdin)

        assert stdin_io.read_raw_stdin() == ""

    def test_unavailable_stdin_returns_empty_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """読み取り不能でも空文字列を返す（非保護経路は fail-open のまま）。

        保護 hook 側は `read_raw_stdin_with_truncation` の
        `StdinUnavailableError` を捕捉して deny に倒す（docs/adr/hook-failure-direction.md）。両者を
        同じ関数にしないのは、launcher の `--bg` 中継や `mem context` には
        「読めなかった」ときに採れる別の行動が無いため。
        """
        default_timeout = stdin_io.STDIN_FIRST_BYTE_TIMEOUT
        monkeypatch.setattr(stdin_io, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(stdin_io.sys, "stdin", _BlockingStdin([]))

        started = time.perf_counter()
        result = stdin_io.read_raw_stdin()
        elapsed = time.perf_counter() - started

        assert result == ""
        # 既定値（2 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
        assert elapsed < default_timeout


class TestReadStdinBytesChunkedDeadline:
    """`_read_stdin_bytes` のチャンクループ・デッドライン挙動のテスト。

    回帰防止: `.buffer.read(max_bytes)` 1 回きりの読み取りは、書き手が
    `max_bytes` に満たないデータしか送らず接続も閉じない場合に無期限へ
    ブロックする。実装はブロッキング read をワーカースレッドへ隔離し、
    本スレッドは queue のタイムアウトで打ち切る（select は使わない）。
    """

    def test_multi_chunk_read_reassembles_full_bytes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """小さい STDIN_CHUNK_BYTES でも複数チャンクを結合して全データを返す。"""
        monkeypatch.setattr(stdin_io, "STDIN_CHUNK_BYTES", 4)
        queue_stdin = _QueueStdin([b"abcd", b"efgh", b"ij"])
        monkeypatch.setattr(stdin_io.sys, "stdin", queue_stdin)

        assert stdin_io._read_stdin_bytes(10) == b"abcdefghij"
        # 要求量が 4, 4, 残り 2。定数が使われなければ 10, 6, 2 になる。
        assert queue_stdin.buffer.requested == [4, 4, 2]

    def test_chunk_request_size_respects_max_bytes_boundary(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """各 read1 の要求量が `min(STDIN_CHUNK_BYTES, max_bytes - collected)` を厳守する。

        `_QueueBuffer` は要求された `n` を実際に守ってスライスするため、
        呼び出し側が `min` を `max` と取り違えると 2 周目で `max_bytes` を
        超えるバイト列を集めてしまい、ここで検出できる。
        """
        monkeypatch.setattr(stdin_io, "STDIN_CHUNK_BYTES", 4)
        queue_stdin = _QueueStdin([b"ab", b"cdefghij"])
        monkeypatch.setattr(stdin_io.sys, "stdin", queue_stdin)

        result = stdin_io._read_stdin_bytes(5)

        assert result == b"abcde"
        # 1 回目は min(4, 5)、2 回目は min(4, 5 - 2)。定数が使われなければ 1 回目が 5 になる。
        assert queue_stdin.buffer.requested == [4, 3]

    def test_single_chunk_fast_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """全データが 1 回のチャンク読みで収まる場合もそのまま返す。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _QueueStdin([b"hello"]))

        assert stdin_io._read_stdin_bytes(10) == b"hello"

    def test_eof_before_max_bytes_returns_partial_data_without_warning(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """書き手が max_bytes 未満で接続を閉じた（EOF）場合、警告なしで部分データを返す。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _QueueStdin([b"abc"]))

        result = stdin_io._read_stdin_bytes(10)

        assert result == b"abc"
        assert capsys.readouterr().err == ""

    def test_stalled_writer_after_first_chunk_returns_partial_and_warns(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """1 チャンク受信後に書き手が止まったら、予算切れで部分データを返し警告する。"""
        default_deadline = stdin_io.STDIN_READ_DEADLINE_SECONDS
        monkeypatch.setattr(stdin_io, "STDIN_READ_DEADLINE_SECONDS", 0.05)
        monkeypatch.setattr(stdin_io.sys, "stdin", _BlockingStdin([b"abc"]))

        started = time.perf_counter()
        result = stdin_io._read_stdin_bytes(10)
        elapsed = time.perf_counter() - started

        assert result == b"abc"
        # 既定値（3 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
        assert elapsed < default_deadline
        assert "上限時間に達した" in capsys.readouterr().err

    def test_first_byte_timeout_raises_unavailable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """最初のバイトが届かないまま予算切れになったら例外を送出する。

        ここで空文字列を返すと、保護 hook が payload を 1 バイトも
        読まないまま許可側へ抜ける（fail-open）。
        """
        default_timeout = stdin_io.STDIN_FIRST_BYTE_TIMEOUT
        monkeypatch.setattr(stdin_io, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(stdin_io.sys, "stdin", _BlockingStdin([]))

        started = time.perf_counter()
        with pytest.raises(stdin_io.StdinUnavailableError, match="0.05 秒以内"):
            stdin_io._read_stdin_bytes(10)
        # 既定値（2 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
        assert time.perf_counter() - started < default_timeout

    @pytest.mark.parametrize("error", [OSError("bad fd"), ValueError("closed file")])
    def test_read1_failure_before_any_data_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        """1 バイトも読めていない状態での read1 例外は「読めなかった」として送出する。

        Windows の通常パイプで select が OSError になる経路と同型で、
        ここを空文字列へ正規化すると allow と deny が同じ exit 0 になる。
        """

        class _RaisingBuffer:
            def read1(self, n: int = -1) -> bytes:
                raise error

        class _RaisingStdin:
            buffer = _RaisingBuffer()

            def isatty(self) -> bool:
                return False

        monkeypatch.setattr(stdin_io.sys, "stdin", _RaisingStdin())

        with pytest.raises(stdin_io.StdinUnavailableError):
            stdin_io._read_stdin_bytes(10)

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

        monkeypatch.setattr(stdin_io.sys, "stdin", _RaisingStdin(_RaisingBuffer(b"abc")))

        assert stdin_io._read_stdin_bytes(10) == b"abc"

    def test_no_buffer_read_failure_raises_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """buffer なし stdin の read() が失敗した場合も「読めなかった」として送出する。"""

        class _RaisingTextStdin:
            def read(self, n: int = -1) -> str:
                raise OSError("bad fd")

        monkeypatch.setattr(stdin_io.sys, "stdin", _RaisingTextStdin())

        with pytest.raises(stdin_io.StdinUnavailableError):
            stdin_io._read_stdin_bytes(10)


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

        monkeypatch.setattr(stdin_io.sys, "stdin", _FdStdin(read_fd))
        try:
            assert stdin_io._read_stdin_bytes(100) == b"payload"
        finally:
            os.close(read_fd)

    def test_missing_fileno_falls_back_to_read1(self) -> None:
        """fileno を持たないオブジェクトは read1 で読むこと。"""
        buffer = _QueueBuffer([b"abc"])

        # bound method は同一性ではなく等価性で比較する。
        assert stdin_io._stdin_chunk_reader(buffer) == buffer.read1

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
        assert stdin_io._stdin_chunk_reader(buffer) == buffer.read1


class TestStdinUnreadableMessage:
    """`stdin_unreadable_message` の文面契約。"""

    def test_message_names_the_hook_and_reason(self) -> None:
        """hook 名と理由の両方が deny 理由に載ること（診断可能性）。"""
        message = stdin_io.stdin_unreadable_message("pre:block-no-verify", "boom")

        assert "pre:block-no-verify" in message
        assert "boom" in message
        assert "BLOCKED" in message


class TestReadRawStdinWithTruncation:
    """read_raw_stdin_with_truncation の切り捨て判定・stdin ガードのテスト。"""

    def test_no_truncation_when_within_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上限内の入力はそのまま、切り捨てフラグは False。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", io.StringIO("short"))

        text, truncated = stdin_io.read_raw_stdin_with_truncation()

        assert text == "short"
        assert truncated is False

    def test_truncates_when_exceeding_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """上限超過の入力は切り捨てたうえでフラグを立てる。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin("a" * 20))

        text, truncated = stdin_io.read_raw_stdin_with_truncation(max_bytes=10)

        assert text == "a" * 10
        assert truncated is True

    def test_tty_returns_empty_and_not_truncated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTY 接続は payload なしとして空文字列を返す（例外にしない）。"""
        monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin("payload", tty=True))

        text, truncated = stdin_io.read_raw_stdin_with_truncation()

        assert text == ""
        assert truncated is False

    def test_unreadable_stdin_raises_for_protection_hooks(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """読み取り不能は例外にする（保護 hook が deny へ倒せるようにする）。"""
        default_timeout = stdin_io.STDIN_FIRST_BYTE_TIMEOUT
        monkeypatch.setattr(stdin_io, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
        monkeypatch.setattr(stdin_io.sys, "stdin", _BlockingStdin([]))

        started = time.perf_counter()
        with pytest.raises(stdin_io.StdinUnavailableError, match="0.05 秒以内"):
            stdin_io.read_raw_stdin_with_truncation()
        # 既定値（2 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
        assert time.perf_counter() - started < default_timeout


class TestParseJsonObject:
    """parse_json_object の直接テスト。

    block_no_verify / config_protection は raw が空の場合に
    parse_json_object を呼ばず早期 return するため、
    空・空白のみ入力を渡す経路をここで直接固定する。
    """

    def test_empty_string_returns_none(self) -> None:
        assert stdin_io.parse_json_object("") is None

    def test_whitespace_only_returns_none(self) -> None:
        assert stdin_io.parse_json_object("   \n\t  ") is None

    def test_invalid_json_returns_none(self) -> None:
        assert stdin_io.parse_json_object("{not-json") is None

    def test_valid_json_object_is_parsed(self) -> None:
        assert stdin_io.parse_json_object('{"a": 1}') == {"a": 1}

    def test_valid_json_non_object_returns_none(self) -> None:
        assert stdin_io.parse_json_object("[1, 2, 3]") is None
