"""フック stdin の読み取り（上限・タイムアウト・JSON 化）。

保護 hook 向けの fail-closed な読み口（`read_raw_stdin_with_truncation`）と、
非保護経路（launcher の ``--bg`` 中継・``mem.cli``）向けの空文字列で受ける読み口
（`read_raw_stdin`）を提供する。
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from typing import Any

from claq.hooks.hook_common import write_stderr
from claq.lib.harness import JSON_PARSE_FAILURES

MAX_STDIN_BYTES = 1024 * 1024


class StdinUnavailableError(RuntimeError):
    """stdin に payload があるはずなのに読み取れなかったことを表す例外。

    「payload が無い」（tty 起動・stdin 未接続・即 EOF）とは区別する。前者で保護
    hook が許可へ倒すと、守るべきコマンドが素通りする。呼び出し側に 2 つを取り違え
    させないため、後者だけを例外にする（docs/adr/hook-failure-direction.md）。
    """


# ホストは spawn 直後に stdin へ JSON を書き込むため、最初のバイト到着まで 2 秒
# あれば十分。stdin リダイレクト漏れ（パイプ未接続のまま open）での無期限ブロックを防ぐ。
STDIN_FIRST_BYTE_TIMEOUT = 2.0

# 最初のチャンク受信後のチャンク読み取りループ全体に許す壁時計予算（秒）。
# payload は KB オーダーの JSON であり、正常系では触れない余裕として取る。
STDIN_READ_DEADLINE_SECONDS = 3.0

# 1 回の read 呼び出しで要求する最大バイト数（チャンクサイズ）。
STDIN_CHUNK_BYTES = 65536


def _stdin_is_absent() -> bool:
    """stdin がそもそも存在しない（読む対象が無い）かを判定します。

    ``sys.stdin`` が None か TTY に繋がっている場合を「payload なし」とする。
    ``isatty()`` が例外化する場合は判定できないため False を返し、実際の読み取り側
    （`_read_stdin_bytes`）に判断を委ねる。

    Returns:
        読む対象が無いと確定できれば True。

    Raises:
        例外は発生しません。
    """
    if sys.stdin is None:
        return True
    try:
        return bool(sys.stdin.isatty())
    except (OSError, ValueError, AttributeError):
        return False


def _stdin_chunk_reader(stdin_buffer: Any) -> Any:
    """ワーカースレッドが使う「n バイト読む」関数を選ぶ。

    実 fd があるときは ``os.read()`` を使う。``read1()`` は BufferedReader のロックを
    保持したままブロックするため、本スレッドがタイムアウトで先に進むと、終了時の
    stdin 後始末がロックを取れず ``Fatal Python error: _enter_buffered_busy`` で
    abort し、deny の exit code 2 が失われる。fd を持たないオブジェクト（テストの
    フェイク・io.BytesIO 等）では ``read1()`` へ落とす。

    Args:
        stdin_buffer: 読み取り対象のバイナリストリーム。

    Returns:
        ``reader(n) -> bytes`` の callable。

    Raises:
        例外は発生しません。
    """
    fileno = getattr(stdin_buffer, "fileno", None)
    if fileno is None:
        return stdin_buffer.read1
    try:
        fd = fileno()
    except (OSError, ValueError):
        return stdin_buffer.read1
    return lambda size: os.read(fd, size)


def _pump_stdin(reader: Any, max_bytes: int, sink: queue.Queue) -> None:
    """stdin をチャンク単位で読み、結果を `sink` へ流す（ワーカースレッド本体）。

    ``select.select`` は使わない。Windows では通常のパイプに ``OSError`` を投げ、
    それを「入力なし」と読むと保護 hook が許可側へ抜ける。ブロッキング read を
    ワーカースレッドへ隔離し、呼び出し側が queue のタイムアウトで待つ形にすると、
    3 プラットフォームで同一のロジックになる。各チャンクは ``n`` バイト揃うまで
    待たない読み方で読み、書き手が途中で止まってもデッドライン判定へ戻れるようにする。

    Args:
        reader: ``reader(n) -> bytes`` の読み取り関数（`_stdin_chunk_reader`）。
        max_bytes: 読み取る最大バイト数。
        sink: ``("data", bytes)`` / ``("error", Exception)`` を受け取るキュー。

    Raises:
        例外は発生しません（読み取り例外は sink へ載せます）。
    """
    collected = 0
    try:
        while collected < max_bytes:
            chunk = reader(min(STDIN_CHUNK_BYTES, max_bytes - collected))
            sink.put(("data", chunk))
            if not chunk:
                return
            collected += len(chunk)
    except (OSError, ValueError, AttributeError) as exc:
        # AttributeError は `.buffer` があるのに `.read1` を持たない stdin
        # （pytest の DontReadFromInput 等）。捕らえないとワーカー内で例外が消える。
        sink.put(("error", exc))


def _read_stdin_bytes(max_bytes: int) -> bytes:
    """stdin から最大 `max_bytes` 分をバイト列として読みます。

    ``.buffer`` があればワーカースレッド（`_pump_stdin`）へ read を隔離し、最初の
    チャンクは STDIN_FIRST_BYTE_TIMEOUT 秒、以降は STDIN_READ_DEADLINE_SECONDS 秒の
    予算で待つ。ワーカーは daemon なので、残ってもプロセス終了を妨げない。

    ``.buffer`` が無い場合（io.StringIO 等）は文字数で読んで UTF-8 へ再エンコード
    する。バイト上限を超えうるため、呼び出し側でバイト換算の切り詰めを行う。

    Args:
        max_bytes: 読み取る最大バイト数です（buffer 無し時は最大文字数）。

    Returns:
        読み取ったバイト列。1 バイト以上受け取った後にデッドライン超過・読み取り
        例外・EOF で打ち切った場合は、その時点までの部分データ（壊れた JSON として
        後段が fail-closed に倒す）。

    Raises:
        StdinUnavailableError: 1 バイトも受け取れないまま、読み取りが例外化
            したか最初のバイトが時間内に届かなかった場合。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is None:
        try:
            return sys.stdin.read(max_bytes).encode("utf-8", errors="replace")
        except (OSError, ValueError, AttributeError) as exc:
            raise StdinUnavailableError(f"stdin の読み取りに失敗しました: {exc}") from exc

    sink: queue.Queue = queue.Queue()
    threading.Thread(
        target=_pump_stdin, args=(_stdin_chunk_reader(stdin_buffer), max_bytes, sink), daemon=True
    ).start()

    collected = b""
    deadline: float | None = None
    while len(collected) < max_bytes:
        timeout = STDIN_FIRST_BYTE_TIMEOUT if deadline is None else max(0.0, deadline - time.monotonic())
        try:
            kind, payload = sink.get(timeout=timeout)
        except queue.Empty:
            if not collected:
                raise StdinUnavailableError(
                    f"stdin の最初のバイトが {STDIN_FIRST_BYTE_TIMEOUT} 秒以内に届きませんでした"
                ) from None
            write_stderr(
                "WARNING: stdin 読み取りが上限時間に達したため、"
                "受信済みの部分データで打ち切ります（stdin の書き手が"
                "応答しない可能性）\n"
            )
            break
        if kind == "error":
            if not collected:
                raise StdinUnavailableError(f"stdin の読み取りに失敗しました: {payload}")
            break
        if payload == b"":
            break
        collected += payload
        if deadline is None:
            deadline = time.monotonic() + STDIN_READ_DEADLINE_SECONDS
    return collected


def read_raw_stdin(max_bytes: int = MAX_STDIN_BYTES) -> str:
    """標準入力から生のテキストをバイト単位の上限つきで読み取ります。

    読み取れなかった場合も空文字列を返す。非保護経路（launcher の ``--bg`` 中継・
    ``mem.cli`` の payload 読み取り）専用で、これらは「入力が無い」と「読めない」を
    区別しても採れる別の行動が無い。保護 hook は `read_raw_stdin_with_truncation` を
    使い、`StdinUnavailableError` を捕捉して fail-closed に倒すこと。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取られた文字列（max_bytes バイトで切り捨て済み）。読む対象が
        無い場合・読み取れなかった場合は空文字列。

    Raises:
        例外は発生しません。
    """
    if _stdin_is_absent():
        return ""
    try:
        raw_bytes = _read_stdin_bytes(max_bytes)
    except StdinUnavailableError:
        return ""
    return raw_bytes[:max_bytes].decode("utf-8", errors="replace")


def read_raw_stdin_with_truncation(max_bytes: int = MAX_STDIN_BYTES) -> tuple[str, bool]:
    """標準入力を読み取り、切り捨ての有無を返します（保護 hook 用）。

    読む対象が無い場合（tty 起動・``sys.stdin`` が None・即 EOF）は ``("", False)``。
    呼び出し側の保護 hook は `StdinUnavailableError` を捕捉して deny に倒すこと
    （docs/adr/hook-failure-direction.md）。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取った文字列と、切り捨てが発生したかどうかのタプル。

    Raises:
        StdinUnavailableError: payload があるはずなのに読み取れなかった場合。
    """
    if _stdin_is_absent():
        return "", False
    raw_bytes = _read_stdin_bytes(max_bytes + 1)
    return raw_bytes[:max_bytes].decode("utf-8", errors="replace"), len(raw_bytes) > max_bytes


def stdin_unreadable_message(hook_name: str, reason: object) -> str:
    """stdin 読み取り不能時の deny 理由を組み立てます。

    4 つの保護 hook が同じ文面と方針を使うための単一情報源です。

    Args:
        hook_name: 呼び出し元 hook の識別名。
        reason: 読み取りに失敗した理由（`StdinUnavailableError`）。

    Returns:
        deny 理由の文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"[Hook] BLOCKED: {hook_name} could not read its stdin payload ({reason}). "
        "The tool call cannot be verified, so it is refused rather than allowed "
        "through unchecked."
    )


def parse_json_object(raw: str) -> dict[str, Any] | None:
    """JSON 文字列を辞書としてパースします。

    パース不能は ``JSON_PARSE_FAILURES`` で一括して受ける。``json.JSONDecodeError``
    だけだと、深いネストで送出される ``RecursionError`` が貫通して保護フックが
    exit 1（PreToolUse では non-blocking error ＝ ツールが実行される）へ倒れる。
    None は呼び出し元で「判定不能 → deny」として扱われる fail-closed 側の値。

    Args:
        raw: パース対象の JSON 文字列です。

    Returns:
        パースされた辞書、または失敗時は None を返します。

    Raises:
        例外は発生せず、パースエラー時は None を返します。
    """
    if not raw or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except JSON_PARSE_FAILURES:
        return None
    return data if isinstance(data, dict) else None
