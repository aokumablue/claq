"""上流 API の呼び出しを包む部品: 失敗の分類・モデル向けの文面・再試行・遮断器・ファンアウト。

上流のクライアント（adapters.py）は、失敗を `UpstreamError` で投げる。ここでは HTTP
クライアントの種類に依存しない。
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})


class UpstreamError(Exception):
    """上流 API の失敗。

    Attributes:
        status: HTTP ステータス。接続の失敗・タイムアウトは None。
        detail: 運用者向けの詳細（ログにだけ出す。モデルへは返さない）。
        retry_after: 上流が示した再試行までの秒数。
    """

    def __init__(self, status: int | None, detail: str = "", *, retry_after: float | None = None) -> None:
        """失敗の内容を保持する。

        Args:
            status: HTTP ステータス。接続の失敗・タイムアウトは None。
            detail: 運用者向けの詳細。
            retry_after: 上流が示した再試行までの秒数。
        """
        super().__init__(f"upstream status={status} {detail}".strip())
        self.status = status
        self.detail = detail
        self.retry_after = retry_after

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
    if status in (401, 403):
        return "現在このデータは取得できません。時間を置いても続く場合は管理者に連絡してください。"
    if status == 404:
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
    deadline: float | None = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    clock: Callable[[], float] = time.monotonic,
    jitter: Callable[[], float] = random.random,
) -> T:
    """一時的な失敗に限って、指数バックオフとジッタで再試行する。

    再試行するのは `retryable` な `UpstreamError` だけ（読み取りは冪等なので安全）。
    上流の `retry_after` があればそれに従い、締切を越える待ちになるなら諦める。

    Args:
        operation: 上流を呼ぶ関数。
        attempts: 最大の試行回数（初回を含む）。
        base_delay: 最初の待ち時間（秒）。
        max_delay: 待ち時間の上限（秒）。
        deadline: 締切（`clock` と同じ基準の時刻）。None なら締切なし。
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
            if delay is None:
                delay = min(max_delay, base_delay * 2**attempt) * (0.5 + jitter() / 2)
            if deadline is not None and clock() + delay >= deadline:
                raise
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


class CircuitBreaker:
    """上流ごとの遮断器。続けて失敗したら、しばらく上流を呼ばずにすぐ失敗を返す。"""

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """遮断の条件を設定する。

        Args:
            failure_threshold: 遮断するまでの連続失敗の回数。
            reset_seconds: 遮断してから試しに 1 回通すまでの秒数。
            clock: 単調増加する現在時刻を返す関数。
        """
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    def allow(self) -> bool:
        """今、上流を呼んでよいか。

        Returns:
            閉じている、または遮断から `reset_seconds` が過ぎていれば True。
        """
        if self._opened_at is None:
            return True
        return self._clock() - self._opened_at >= self._reset_seconds

    def record_success(self) -> None:
        """成功を記録し、遮断を解く。"""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """一時的な失敗を記録し、回数が閾値に達したら遮断する。"""
        self._failures += 1
        if self._failures >= self._failure_threshold:
            self._opened_at = self._clock()

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """遮断器を通して上流を呼ぶ。

        数えるのは一時的な失敗（`retryable`）だけ。404 などの業務上の失敗は数えない。

        Args:
            operation: 上流を呼ぶ関数（再試行を含めてよい）。

        Returns:
            `operation` の結果。

        Raises:
            UpstreamError: 遮断中、または上流が失敗したとき。
        """
        if not self.allow():
            raise UpstreamError(503, "circuit open")
        try:
            result = await operation()
        except UpstreamError as error:
            if error.retryable:
                self.record_failure()
            raise
        self.record_success()
        return result


async def fan_out(
    calls: Mapping[str, Callable[[], Awaitable[Any]]],
    *,
    timeout: float,
) -> tuple[dict[str, Any], dict[str, UpstreamError]]:
    """独立した上流呼び出しを並列に出し、締切までに揃った分を返す。

    失敗や締切超えの呼び出しは `failures` に入れ、全体は失敗にしない（部分的な結果で
    答えられる問いは多い）。どの部分が不可欠かは呼び出し側が決める。
    `UpstreamError` 以外の例外はプログラムの誤りなので、そのまま投げる。

    Args:
        calls: 名前 → 上流を呼ぶ関数。
        timeout: 締切までの秒数。

    Returns:
        （名前 → 結果、名前 → 失敗）の組。

    Raises:
        Exception: いずれかの呼び出しが `UpstreamError` 以外の例外を投げたとき。
    """
    tasks = {name: asyncio.ensure_future(call()) for name, call in calls.items()}
    if not tasks:
        return {}, {}
    _, pending = await asyncio.wait(tasks.values(), timeout=timeout)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
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
