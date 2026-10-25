"""stdin_io の stdin 読み取りガード（TTY/タイムアウト/バイト上限）と JSON 化のテスト。"""

from __future__ import annotations

import io
import os
import threading
import time
import types
from collections.abc import Callable
from unittest import mock

import pytest

from claq.hooks import stdin_io


class _FakeStdin:
    """read1 ごとに先頭チャンクを要求量まで返し、尽きた後は ``end`` に従う stdin（``buffer`` は自身）。

    ``end`` は None なら EOF（b""）、``"block"`` なら書き手が送り切らず閉じもしない状態として永久に待ち、
    例外なら送出する。isatty() の呼び出し回数と read1 の要求量を記録する。待ち続けるのは daemon の
    ワーカースレッドなので、プロセス終了は妨げない。
    """

    def __init__(
        self,
        *chunks: bytes,
        tty: bool = False,
        isatty_error: Exception | None = None,
        end: Exception | str | None = None,
    ) -> None:
        """返すチャンク列・TTY 判定（isatty_error があれば送出）・尽きた後の振る舞いを受け取る。"""
        self.buffer = self
        self._chunks = list(chunks)
        self._tty = tty
        self._isatty_error = isatty_error
        self._end = end
        self.isatty_calls = 0
        self.requested: list[int] = []

    def isatty(self) -> bool:
        """呼び出しを数え、指定の TTY 判定を返す（isatty_error があれば送出する）。"""
        self.isatty_calls += 1
        if self._isatty_error is not None:
            raise self._isatty_error
        return self._tty

    def read1(self, n: int = -1) -> bytes:
        """先頭チャンクから最大 n バイトを返し、残りは次回へ回す（実 read1 は要求量を超えて返さない）。"""
        self.requested.append(n)
        if not self._chunks:
            if isinstance(self._end, Exception):
                raise self._end
            if self._end == "block":
                threading.Event().wait()
            return b""
        chunk = self._chunks.pop(0)
        if 0 <= n < len(chunk):
            self._chunks.insert(0, chunk[n:])
            return chunk[:n]
        return chunk


@pytest.mark.parametrize(
    ("make_stdin", "expected"),
    [
        pytest.param(lambda: _FakeStdin(tty=True), True, id="tty"),
        pytest.param(lambda: _FakeStdin(), False, id="pipe"),
        # detach された子プロセス等で sys.stdin が None の場合。
        pytest.param(lambda: None, True, id="stdin-none"),
        # isatty() の例外を「無い」と決めつけると、判定不能を素通りへ倒し Windows のパイプの fail-open を再生産する。
        pytest.param(lambda: _FakeStdin(isatty_error=OSError("bad fd")), False, id="isatty-oserror"),
        pytest.param(lambda: _FakeStdin(isatty_error=ValueError("closed")), False, id="isatty-valueerror"),
        pytest.param(lambda: _FakeStdin(isatty_error=AttributeError("x")), False, id="isatty-attributeerror"),
    ],
)
def test_stdin_is_absent(
    monkeypatch: pytest.MonkeyPatch, make_stdin: Callable[[], _FakeStdin | None], expected: bool
) -> None:
    """TTY と sys.stdin=None だけを payload なしとし、パイプと isatty() の例外は読み取り側の判断へ委ねること。

    select による readiness 判定は Windows のパイプで OSError になり「入力なし」に化けるため使わない。
    """
    stdin = make_stdin()
    monkeypatch.setattr(stdin_io.sys, "stdin", stdin)

    assert stdin_io._stdin_is_absent() is expected
    # pytest 既定の stdin も非 TTY なので、差し替えの到達は isatty() の呼び出し回数で固定する。
    assert stdin is None or stdin.isatty_calls == 1


@pytest.mark.parametrize(
    ("make_stdin", "max_bytes", "raw", "with_truncation"),
    [
        # 10 バイト = 「あ」3 文字（9 バイト）+ 切断された 1 バイト（置換文字）。buffer の無い stdin もバイト換算で切る。
        pytest.param(
            lambda: _FakeStdin(("あ" * 10).encode()), 10, "あああ�", ("あああ�", True), id="buffer-cut-by-bytes"
        ),
        pytest.param(lambda: io.StringIO("あ" * 10), 10, "あああ�", ("あああ�", True), id="text-cut-by-bytes"),
        pytest.param(lambda: _FakeStdin(b"a" * 20), 10, "a" * 10, ("a" * 10, True), id="over-limit"),
        pytest.param(
            lambda: io.StringIO("short"), stdin_io.MAX_STDIN_BYTES, "short", ("short", False), id="within-limit"
        ),
        # TTY 接続（人手による直接起動）は読まずに payload なしとして扱う（例外にしない）。
        pytest.param(
            lambda: _FakeStdin(b"payload", tty=True), stdin_io.MAX_STDIN_BYTES, "", ("", False), id="tty"
        ),
    ],
)
def test_read_raw_stdin_and_with_truncation(
    monkeypatch: pytest.MonkeyPatch,
    make_stdin: Callable[[], object],
    max_bytes: int,
    raw: str,
    with_truncation: tuple[str, bool],
) -> None:
    """2 つの読み口がバイト単位の上限で切り（保護 hook 用は切り捨ての有無も返す）、TTY は読まずに空にすること。"""
    monkeypatch.setattr(stdin_io.sys, "stdin", make_stdin())
    assert stdin_io.read_raw_stdin(max_bytes) == raw

    monkeypatch.setattr(stdin_io.sys, "stdin", make_stdin())
    assert stdin_io.read_raw_stdin_with_truncation(max_bytes) == with_truncation


def test_unreadable_stdin_is_empty_for_raw_reader_and_raises_for_protection(monkeypatch: pytest.MonkeyPatch) -> None:
    """最初のバイトが届かない stdin は、非保護の読み口では空文字列、保護 hook の読み口では例外になること。

    非保護経路（launcher の `--bg` 中継・`mem context`）には「読めなかった」ときに採れる別の行動が無いため空で受け、
    保護 hook は `StdinUnavailableError` を捕捉して deny に倒す（docs/adr/hook-failure-direction.md）。
    ここで空文字列を返すと、保護 hook が payload を 1 バイトも読まないまま許可側へ抜ける。
    """
    default_timeout = stdin_io.STDIN_FIRST_BYTE_TIMEOUT
    monkeypatch.setattr(stdin_io, "STDIN_FIRST_BYTE_TIMEOUT", 0.05)
    started = time.perf_counter()

    monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin(end="block"))
    assert stdin_io.read_raw_stdin() == ""
    monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin(end="block"))
    with pytest.raises(stdin_io.StdinUnavailableError, match="0.05 秒以内"):
        stdin_io.read_raw_stdin_with_truncation()

    # 既定値（2 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
    assert time.perf_counter() - started < default_timeout


@pytest.mark.parametrize(
    ("chunks", "max_bytes", "expected", "requested"),
    [
        # 要求量は 4, 4, 残り 2。定数が使われなければ 10, 6, 2 になる。
        pytest.param([b"abcd", b"efgh", b"ij"], 10, b"abcdefghij", [4, 4, 2], id="multi-chunk"),
        # 1 回目は min(4, 5)、2 回目は min(4, 5 - 2)。min を max と取り違えると 2 周目で max_bytes を超えて集める。
        pytest.param([b"ab", b"cdefghij"], 5, b"abcde", [4, 3], id="request-capped-by-remaining"),
    ],
)
def test_read_stdin_bytes_requests_bounded_chunks(
    monkeypatch: pytest.MonkeyPatch, chunks: list[bytes], max_bytes: int, expected: bytes, requested: list[int]
) -> None:
    """各 read1 の要求量が `min(STDIN_CHUNK_BYTES, max_bytes - 受信済み)` を守り、チャンクを結合して返すこと。

    回帰防止: `.buffer.read(max_bytes)` 1 回きりの読み取りは、書き手が max_bytes に満たないまま接続も閉じないと
    無期限にブロックする。実装はブロッキング read をワーカースレッドへ隔離し、queue のタイムアウトで打ち切る。
    """
    monkeypatch.setattr(stdin_io, "STDIN_CHUNK_BYTES", 4)
    stdin = _FakeStdin(*chunks)
    monkeypatch.setattr(stdin_io.sys, "stdin", stdin)

    assert stdin_io._read_stdin_bytes(max_bytes) == expected
    assert stdin.requested == requested


@pytest.mark.parametrize(
    ("end", "warns"),
    [(None, False), ("block", True), (ValueError("I/O operation on closed file"), False)],
    ids=["eof", "writer-stalls", "read1-fails"],
)
def test_read_stdin_bytes_returns_partial_data_after_first_chunk(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    end: Exception | str | None,
    warns: bool,
) -> None:
    """1 チャンク受信後の EOF・書き手の停止・読み取り例外は、その部分データで打ち切ること（停止のときだけ警告する）。

    壊れた JSON として後段の `parse_json_object` が None を返し、保護 hook はそこで fail-closed に倒す。
    """
    default_deadline = stdin_io.STDIN_READ_DEADLINE_SECONDS
    monkeypatch.setattr(stdin_io, "STDIN_READ_DEADLINE_SECONDS", 0.05)
    monkeypatch.setattr(stdin_io.sys, "stdin", _FakeStdin(b"abc", end=end))
    started = time.perf_counter()

    assert stdin_io._read_stdin_bytes(10) == b"abc"
    # 既定値（3 秒）のままなら待ち時間は必ずそれ以上になる。短縮値が使われたことを時間で固定する。
    assert time.perf_counter() - started < default_deadline
    assert ("上限時間に達した" in capsys.readouterr().err) is warns


@pytest.mark.parametrize(
    "stdin",
    [
        # Windows の通常パイプで select が OSError になる経路と同型。空へ正規化すると allow と deny が同じ exit 0 になる。
        pytest.param(_FakeStdin(end=OSError("bad fd")), id="read1-oserror"),
        pytest.param(_FakeStdin(end=ValueError("closed file")), id="read1-valueerror"),
        # buffer を持たない stdin の read() 失敗も同じ扱い。
        pytest.param(mock.Mock(spec=["read"], **{"read.side_effect": OSError("bad fd")}), id="text-read-oserror"),
    ],
)
def test_read_failure_before_any_data_raises_unavailable(monkeypatch: pytest.MonkeyPatch, stdin: object) -> None:
    """1 バイトも受け取らないうちの読み取り例外は「読めなかった」として StdinUnavailableError を送出すること。"""
    monkeypatch.setattr(stdin_io.sys, "stdin", stdin)

    with pytest.raises(stdin_io.StdinUnavailableError):
        stdin_io._read_stdin_bytes(10)


def test_real_fd_is_read_without_buffer_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    """実 fd を持つ stdin は read1 ではなく os.read で読むこと。

    `BufferedReader.read1()` はロックを握ったままブロックし、書き手が止まったまま本スレッドが先へ進むと、
    終了時の stdin 後始末が `Fatal Python error: _enter_buffered_busy` で abort する（実測）。os.read は
    Python レベルのロックを握らない。
    """
    read_fd, write_fd = os.pipe()
    os.write(write_fd, b"payload")
    os.close(write_fd)
    # read1 を持たせないので、read1 の経路へ落ちれば AttributeError で失敗する。
    stdin = types.SimpleNamespace(buffer=types.SimpleNamespace(fileno=lambda: read_fd))
    monkeypatch.setattr(stdin_io.sys, "stdin", stdin)
    try:
        assert stdin_io._read_stdin_bytes(100) == b"payload"
    finally:
        os.close(read_fd)


@pytest.mark.parametrize(
    "buffer",
    [
        pytest.param(mock.Mock(spec=["read1"]), id="no-fileno"),
        pytest.param(
            mock.Mock(spec=["fileno", "read1"], **{"fileno.side_effect": OSError("no fd")}), id="fileno-oserror"
        ),
        pytest.param(
            mock.Mock(spec=["fileno", "read1"], **{"fileno.side_effect": ValueError("closed")}), id="fileno-valueerror"
        ),
    ],
)
def test_buffer_without_usable_fd_falls_back_to_read1(buffer: mock.Mock) -> None:
    """fileno を持たない・fileno() が例外化するオブジェクト（テストのフェイク・io.BytesIO 等）は read1 で読むこと。"""
    assert stdin_io._stdin_chunk_reader(buffer) is buffer.read1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("", None), ("   \n\t  ", None), ("{not-json", None), ('{"a": 1}', {"a": 1}), ("[1, 2, 3]", None)],
    ids=["empty", "whitespace-only", "invalid-json", "object", "non-object"],
)
def test_parse_json_object(raw: str, expected: dict | None) -> None:
    """dict の JSON だけを返し、空・空白のみ・壊れた JSON・dict 以外は None にすること。

    block_no_verify / config_protection は空入力で parse_json_object を呼ばずに早期 return するため、
    空・空白のみの経路はここで直接固定する。
    """
    assert stdin_io.parse_json_object(raw) == expected
