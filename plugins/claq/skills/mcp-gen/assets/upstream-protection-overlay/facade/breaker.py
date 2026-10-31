"""上流ごとの遮断器と、遮断器を通した締切付きの呼び出し（upstream-protection-overlay）。

続けて失敗する上流を、しばらく呼ばずにすぐ失敗にする。上流の契約に上限がある・共有のリモートで
多くの主体が同じ上流を呼ぶときに重ねる。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

from .upstream import UpstreamError, call_with_deadline


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

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """遮断器を通して上流を呼ぶ。

        遮断中（遮断から `reset_seconds` が過ぎるまで）は上流を呼ばない。数えるのは一時的な失敗
        （`retryable`）だけで、404 などの業務上の失敗は数えない。成功したら数を戻し、遮断を解く。

        Args:
            operation: 上流を呼ぶ関数（再試行を含めてよい）。

        Returns:
            `operation` の結果。

        Raises:
            UpstreamError: 遮断中、または上流が失敗したとき。
        """
        if self._opened_at is not None and self._clock() - self._opened_at < self._reset_seconds:
            raise UpstreamError(503, "circuit open")
        try:
            result = await operation()
        except UpstreamError as error:
            if error.retryable:
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._opened_at = self._clock()
            raise
        self._failures = 0
        self._opened_at = None
        return result


async def call_upstream[T](
    breaker: CircuitBreaker,
    operation: Callable[[], Awaitable[T]],
    *,
    deadline: float,
    clock: Callable[[], float] = time.monotonic,
) -> T:
    """遮断器と再試行を通して上流を呼び、再試行を含む全体を締切で打ち切る。

    締切は遮断器の内側で掛けるので、締切超えも遮断器の失敗として数えられる。

    Args:
        breaker: 上流ごとの遮断器。
        operation: 上流を呼ぶ関数。
        deadline: 締切（`clock` と同じ基準の時刻）。
        clock: 単調増加する現在時刻を返す関数。

    Returns:
        上流の結果。

    Raises:
        UpstreamError: 上流が失敗した・遮断中・締切を越えた（status は None）とき。
    """
    return await breaker.call(lambda: call_with_deadline(operation, deadline=deadline, clock=clock))
