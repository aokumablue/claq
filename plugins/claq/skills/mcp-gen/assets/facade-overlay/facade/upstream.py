"""上流 API の呼び出しを包む部品: 失敗の分類・モデル向けの文面・再試行・締切付きの呼び出し・ファンアウト。

上流のクライアント（adapters.py）は、失敗を `UpstreamError` で投げる。ここでは HTTP
クライアントの種類に依存しない。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
DETAIL_MAX_CHARS = 200

logger = logging.getLogger(__name__)


def _loggable(detail: str) -> str:
    """detail を、ログに出してよい形にする。

    制御文字（CR/LF を含む）・書式文字などの C 区分の文字を除き（ログの行の偽造と端末の
    制御列を防ぐ）、`DETAIL_MAX_CHARS` 文字に切る。

    Args:
        detail: 運用者向けの詳細。

    Returns:
        整えた詳細。
    """
    return "".join(char for char in detail if not unicodedata.category(char).startswith("C"))[:DETAIL_MAX_CHARS]


class UpstreamError(Exception):
    """上流 API の失敗。

    detail には URL のクエリ・ヘッダ・応答本文を入れない（秘密や個人情報が混じる）。
    ステータスでは分からない短い事情（操作名・上流のエラーコードなど）だけを書く。
    detail はログに出してよい形に整えてから持ち、例外の文面もその値から作る
    （どの経路でログに出ても、改行や制御列が混じらない）。

    Attributes:
        status: HTTP ステータス。接続の失敗・タイムアウトは None。
        detail: 運用者向けの詳細（整えた後の値。ログにだけ出す。モデルへは返さない）。
        retry_after: 上流が示した再試行までの秒数。
    """

    def __init__(self, status: int | None, detail: str = "", *, retry_after: float | None = None) -> None:
        """失敗の内容を保持する。

        Args:
            status: HTTP ステータス。接続の失敗・タイムアウトは None。
            detail: 運用者向けの詳細。制御文字を除き、200 文字に切って持つ。
            retry_after: 上流が示した再試行までの秒数。
        """
        self.status = status
        self.detail = _loggable(detail)
        self.retry_after = retry_after
        super().__init__(f"upstream status={status} {self.detail}".strip())

    @property
    def retryable(self) -> bool:
        """時間を置けば成功しうる失敗か。"""
        return self.status is None or self.status in RETRYABLE_STATUSES


def model_message(error: UpstreamError) -> str:
    """上流の失敗を、モデルに読ませる文面にする（内部の情報は含めない）。

    Args:
        error: 上流の失敗。

    Returns:
        何が起きたかと次に何をすればよいかを書いた文面。
    """
    status = error.status
    if status == 400:
        return "上流が条件を受け付けませんでした。引数の値と組み合わせを見直してください。"
    if status == 401:
        return "現在このデータは取得できません。時間を置いても続く場合は管理者に連絡してください。"
    if status in (403, 404):
        # 403 も 404 と同じ文面にする（「権限が無い」と答えると、対象が存在することを漏らす）
        return "見つからないか、閲覧の権限がありません。検索ツールで ID を確かめてください。"
    if status in (409, 412):
        return "データが更新中です。取得し直してください。"
    if status == 429:
        return "混み合っていて取得できません。条件を絞るか、時間を置いてから呼び直してください。"
    return "一時的に取得できません。時間を置いてから呼び直してください。"


async def call_with_retry[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay: float = 0.2,
    max_delay: float = 2.0,
    max_retry_after: float = 5.0,
    deadline: float,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] = random.random,
) -> T:
    """一時的な失敗に限って、指数バックオフとジッタで再試行する。

    再試行するのは `retryable` な `UpstreamError` だけ。出所: 429・503 は HTTP の契約で再試行を
    求める応答（Retry-After）で、502・504・接続失敗は冪等な読み取りなら繰り返しても結果が変わらない。
    再試行するたびに警告ログを残す（続行したことを運用者に見せる）。
    上流の `retry_after` があればそれに従う。ただし `max_retry_after` を超える待ちと、
    締切を越える待ちはせずに諦める。

    Args:
        operation: 上流を呼ぶ関数。
        attempts: 最大の試行回数（初回を含む）。
        base_delay: 最初の待ち時間（秒）。
        max_delay: 待ち時間の上限（秒）。
        max_retry_after: 上流の `retry_after` に従って待つ上限（秒）。超えるなら待たずに失敗を投げる。
        deadline: 締切（`clock` と同じ基準の時刻）。
        sleep: 待つ関数。
        clock: 単調増加する現在時刻を返す関数。
        jitter: 0 以上 1 未満の乱数を返す関数。

    Returns:
        `operation` の結果。

    Raises:
        UpstreamError: 再試行しない失敗、または試行を使い切ったとき。
    """
    for attempt in range(attempts):
        try:
            return await operation()
        except UpstreamError as error:
            if not error.retryable or attempt == attempts - 1:
                raise
            delay = error.retry_after
            # 負・NaN・上限超えの待ちは上流の誤りとして諦める（NaN は比較がすべて偽になり、ここで落ちる）。
            if delay is not None and not 0 <= delay <= max_retry_after:
                raise
            if delay is None:
                delay = min(max_delay, base_delay * 2**attempt) * (0.5 + jitter() / 2)
            if clock() + delay >= deadline:
                raise
            logger.warning(
                "upstream retry: attempt=%d/%d delay=%.2fs status=%s %s",
                attempt + 1,
                attempts,
                delay,
                error.status,
                error.detail,
            )
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def call_with_deadline[T](
    operation: Callable[[], Awaitable[T]],
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """再試行を通して上流を呼び、再試行を含む全体を締切で打ち切る。

    締切を過ぎたら operation を取り消す（operation の後始末は取り消しの中で走る）。

    Args:
        operation: 上流を呼ぶ関数。
        deadline: 締切（`clock` と同じ基準の時刻）。
        clock: 単調増加する現在時刻を返す関数。

    Returns:
        上流の結果。

    Raises:
        UpstreamError: 上流が失敗した、または締切を越えた（status は None）とき。
    """
    try:
        async with asyncio.timeout(max(0.0, deadline - clock())):
            return await call_with_retry(operation, deadline=deadline, clock=clock)
    except TimeoutError as error:
        raise UpstreamError(None, "deadline exceeded") from error


async def fan_out(
    calls: Mapping[str, Callable[[], Awaitable[Any]]],
    *,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, UpstreamError]]:
    """独立した上流呼び出しを並列に出し、締切までに揃った分を返す。

    失敗や締切超えの呼び出しは `failures` に入れ、全体は失敗にしない（部分的な結果で
    答えられる問いは多い）。どの部分が不可欠かは呼び出し側が決める。
    `UpstreamError` 以外の例外はプログラムの誤りなので、そのまま投げる。
    待っている間に呼び出し元が取り消されたら、終わっていない呼び出しを取り消し、止まるのを
    待ってから取り消しを伝える（上流の呼び出しを締切まで走らせ続けない）。

    Args:
        calls: 名前 → 上流を呼ぶ関数。
        timeout: 締切までの秒数。

    Returns:
        （名前 → 結果、名前 → 失敗）の組。

    Raises:
        asyncio.CancelledError: 待っている間に呼び出し元が取り消されたとき。
        Exception: いずれかの呼び出しが `UpstreamError` 以外の例外を投げたとき。
    """
    if not calls:  # 呼ぶ上流を条件で組み立てると空になりうる（asyncio.wait は空の集合を受け付けない）
        return {}, {}
    tasks = {name: asyncio.ensure_future(call()) for name, call in calls.items()}
    try:
        _, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    finally:
        # 締切を越えた呼び出しと、待っている間に呼び出し元が取り消されたときの全ての呼び出しを止め、
        # 止まるまで待つ（取り消しを上流の呼び出しまで伝える）
        unfinished = [task for task in tasks.values() if not task.done()]
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
    results: dict[str, Any] = {}
    failures: dict[str, UpstreamError] = {}
    for name, task in tasks.items():
        if task in pending:
            failures[name] = UpstreamError(None, "deadline exceeded")
            continue
        error = task.exception()
        if error is None:
            results[name] = task.result()
        elif isinstance(error, UpstreamError):
            failures[name] = error
        else:
            raise error
    return results, failures
