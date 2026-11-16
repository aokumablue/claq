"""上流ごとの遮断器と、遮断器を通した締切付きの呼び出し（upstream-protection-overlay）。

続けて失敗する上流を、しばらく呼ばずにすぐ失敗にする。上流の契約に上限がある・共有のリモートで
多くの主体が同じ上流を呼ぶときに重ねる。
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable

from .upstream import UpstreamError, call_with_deadline


class CircuitBreaker:
    """上流ごとの遮断器。上流を呼ばない期間を決め、その間はすぐ失敗を返す。

    呼ばない期間は 2 通りで決まる。上流が `retry_after` を示したら、回数を待たずにその時刻まで
    （HTTP の Retry-After は、次の要求まで待つ時間を上流が指定する契約）。示さない一時的な失敗は、
    `failure_threshold` 回続いたら `reset_seconds` の間。
    """

    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """遮断の条件を設定する。

        Args:
            failure_threshold: 遮断するまでの連続失敗の回数。1 回の失敗は再試行を使い切った 1 回の
                呼び出し。2 以上にする（1 だと、重い 1 件の締切超えで全主体の呼び出しが止まる）。
            reset_seconds: 上流が `retry_after` を示さないときに、遮断してから次に通すまでの秒数。
                短いほど回復に早く気づき、長いほど落ちている上流への試行が減る。
            clock: 単調増加する現在時刻を返す関数。
        """
        self._failure_threshold = failure_threshold
        self._reset_seconds = reset_seconds
        self._clock = clock
        self._failures = 0
        self._open_until: float | None = None

    def _hold(self, seconds: float) -> None:
        """今から seconds の間、上流を呼ばないことにする（すでに長く止めていれば、そのままにする）。

        Args:
            seconds: 呼ばない秒数。
        """
        until = self._clock() + seconds
        if self._open_until is None or until > self._open_until:
            self._open_until = until

    async def call[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        """遮断器を通して上流を呼ぶ。

        遮断中は上流を呼ばない。扱うのは一時的な失敗（`retryable`）だけで、404 などの業務上の失敗は
        数えない。成功したら数を戻し、遮断を解く。

        Args:
            operation: 上流を呼ぶ関数（再試行を含めてよい）。

        Returns:
            `operation` の結果。

        Raises:
            UpstreamError: 遮断中、または上流が失敗したとき。
        """
        if self._open_until is not None and self._clock() < self._open_until:
            raise UpstreamError(503, "circuit open")
        try:
            result = await operation()
        except UpstreamError as error:
            if error.retryable:
                # 上流の文字列から作った値なので、有限の正の数だけを待つ時間として扱う（inf は遮断が明けなくなる）
                if error.retry_after is not None and math.isfinite(error.retry_after) and error.retry_after > 0:
                    self._hold(error.retry_after)
                self._failures += 1
                if self._failures >= self._failure_threshold:
                    self._hold(self._reset_seconds)
            raise
        self._failures = 0
        self._open_until = None
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
