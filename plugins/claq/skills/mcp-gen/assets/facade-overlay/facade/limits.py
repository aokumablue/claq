"""上流の呼び出しの量を絞る部品: サーバ自身の絞り・上流のクォータ・同時実行の上限・同じ要求の合流と、それらをまとめた上流ごとの方針。

上流から 429 を受ける前に、サーバの側で呼び出しの量を絞る。絞るときは待たずにすぐ失敗を返す
（待てば締切を越え、締切超えが遮断器の失敗として数えられる）。server.py は上流ごとに
`UpstreamPolicy` を 1 つ置き、上流の呼び出しを全てその `call` に通す。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any

from .upstream import CircuitBreaker, UpstreamError, call_upstream


class Throttled(UpstreamError):
    """サーバ自身が上流の呼び出しを絞った（上流は呼んでいない）。

    モデルへの文面は上流の 429 と同じ（条件を絞るか、時間を置く）。再試行はしない。
    すぐ叩き直しても同じ絞りに当たるだけで、遮断器もこれを上流の失敗として数えない
    （サーバ自身の絞りで遮断器を開くと、上流が健全でも全ての呼び出しが止まる）。
    """

    def __init__(self, detail: str) -> None:
        """絞った理由を保持する。

        Args:
            detail: 運用者向けの詳細（どの上限に当たったか）。
        """
        super().__init__(429, detail)

    @property
    def retryable(self) -> bool:
        """再試行の対象にしない（`call_with_retry` も `CircuitBreaker` も扱わない）。"""
        return False


class UpstreamQuota:
    """上流ごとのトークンバケット。全主体で共有して数える。"""

    def __init__(self, rate_per_second: float, burst: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        """回復の速さと、貯められる上限を設定する（満タンで始める）。

        Args:
            rate_per_second: 1 秒あたりに回復する呼び出しの回数。
            burst: 貯められる呼び出しの回数の上限。
            clock: 単調増加する現在時刻を返す関数。
        """
        self._rate = rate_per_second
        self._burst = burst
        self._clock = clock
        self._tokens = float(burst)
        self._updated_at = clock()

    def take(self) -> None:
        """上流を 1 回呼ぶぶんを使う。足りなければ待たずに失敗する。

        Raises:
            Throttled: 使える回数が残っていないとき。
        """
        now = self._clock()
        self._tokens = min(self._burst, self._tokens + (now - self._updated_at) * self._rate)
        self._updated_at = now
        if self._tokens < 1:
            raise Throttled("upstream quota")
        self._tokens -= 1


class ConcurrencyLimit:
    """上流ごとの同時実行の上限。上流の総数と、主体ごとの数の 2 系統で数える。

    主体ごとの上限は、1 つの主体が上流の枠を使い切って他の主体を締め出さないために置く。
    """

    def __init__(self, max_concurrent: int, max_concurrent_per_principal: int) -> None:
        """上限を設定する。

        Args:
            max_concurrent: 全主体を合わせた同時実行の上限。
            max_concurrent_per_principal: 1 つの主体の同時実行の上限。
        """
        self._max_concurrent = max_concurrent
        self._max_per_principal = max_concurrent_per_principal
        self._running = 0
        self._per_principal: dict[str, int] = {}

    @contextmanager
    def slot(self, principal: str) -> Iterator[None]:
        """枠を 1 つ取り、ブロックを抜けたら（例外でも）返す。空きが無ければ待たずに失敗する。

        Args:
            principal: 呼び出しの主体。

        Yields:
            なし（with ブロックの中で上流を呼ぶ）。

        Raises:
            Throttled: 上流の総数か、主体の数が上限に達しているとき。
        """
        held = self._per_principal.get(principal, 0)
        if self._running >= self._max_concurrent or held >= self._max_per_principal:
            raise Throttled("concurrency limit")
        self._running += 1
        self._per_principal[principal] = held + 1
        try:
            yield
        finally:
            self._running -= 1
            remaining = self._per_principal[principal] - 1
            # 枠を全て返した主体の記録は消す（主体の数だけ記録が増え続けない）
            if remaining:
                self._per_principal[principal] = remaining
            else:
                del self._per_principal[principal]


class _Flight:
    """1 本にまとめた呼び出しと、それを待っている呼び出し元の数。"""

    def __init__(self, task: asyncio.Task[Any]) -> None:
        """共有の呼び出しを保持する（待ち手はまだいない）。

        Args:
            task: 共有の呼び出し。
        """
        self.task = task
        self.waiters = 0


class SingleFlight:
    """同じキーの同時の呼び出しを、1 本の上流呼び出しにまとめる。

    まとめるのは実行中の呼び出しだけで、結果は残さない（キャッシュではない）。終わった直後に
    同じキーで呼べば、上流を呼び直す。
    """

    def __init__(self) -> None:
        """実行中の呼び出しの表を空にする。"""
        self._flights: dict[Hashable, _Flight] = {}

    async def run[T](self, key: Hashable, operation: Callable[[], Awaitable[T]]) -> T:
        """キーの呼び出しが実行中ならそれに合流し、無ければ operation を呼ぶ。

        合流した呼び出し元が取り消されても、共有の呼び出しは止めない（残りの待ち手が使う）。
        最後の待ち手が取り消されたら、キーを表から外してから共有の呼び出しを取り消し、止まるのを
        待ってから取り消しを伝える（誰も待たない上流の呼び出しを走らせ続けない）。先に外すのは、
        上流の後始末が終わるまでの間に同じキーで来た呼び出しを、取り消し中の呼び出しに合流させず
        上流を呼び直させるため（合流すると、取り消されていない呼び出し元が取り消しを受け取る）。

        Args:
            key: 同じ要求を見分けるキー。
            operation: 上流を呼ぶ関数（合流したときは呼ばない）。

        Returns:
            共有の呼び出しの結果。

        Raises:
            asyncio.CancelledError: 待っている間に呼び出し元が取り消されたとき。
            Exception: 共有の呼び出しが投げた例外（合流した全員に同じものを投げる）。
        """
        flight = self._flights.get(key)
        if flight is None:
            flight = self._flights[key] = _Flight(asyncio.ensure_future(self._shared(key, operation)))
        flight.waiters += 1
        try:
            return await asyncio.shield(flight.task)
        except asyncio.CancelledError:
            flight.waiters -= 1
            if not flight.waiters:
                # 共有の呼び出しが同じ巡で終わっていれば、キーは外れ済みか、新しい呼び出しを指している
                if self._flights.get(key) is flight:
                    del self._flights[key]
                flight.task.cancel()
                await asyncio.gather(flight.task, return_exceptions=True)
            raise

    async def _shared[T](self, key: Hashable, operation: Callable[[], Awaitable[T]]) -> T:
        """共有の呼び出しの本体。終われば（失敗・取り消しでも）、表がまだこの呼び出しを指すときだけキーを外す。

        キーを外すのは、自分で終わるときはこの finally の中、最後の待ち手が取り消したときは run の中で
        取り消しより前。どちらも task が終わる前なので、終わった task にも取り消し中の task にも合流する
        呼び出しは無い。run が先に外した後は、同じキーの新しい呼び出しが表にいることがあり、それは消さない。

        Args:
            key: 同じ要求を見分けるキー。
            operation: 上流を呼ぶ関数。

        Returns:
            operation の結果。
        """
        try:
            return await operation()
        finally:
            if (flight := self._flights.get(key)) is not None and flight.task is asyncio.current_task():
                del self._flights[key]


class UpstreamPolicy:
    """上流ごとの呼び出しの方針。上流 1 つにつき 1 つ作り、全主体で共有する。

    同じ要求の合流（`SingleFlight`）→ 同時実行の上限（`ConcurrencyLimit`）→ 遮断器・再試行・締切
    （`call_upstream`）→ 試行ごとのクォータ（`UpstreamQuota`）の順に通す。クォータは試行ごとに数える
    （再試行も上流を呼ぶ）。遮断中は上流を呼ばないので、クォータを使わない。
    """

    def __init__(
        self,
        *,
        rate_per_second: float,
        burst: int,
        max_concurrent: int,
        max_concurrent_per_principal: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """上限を設定する。

        Args:
            rate_per_second: 上流のクォータが 1 秒あたりに回復する回数。
            burst: 上流のクォータを貯められる回数の上限。
            max_concurrent: この上流への全主体を合わせた同時実行の上限。
            max_concurrent_per_principal: この上流への 1 つの主体の同時実行の上限。
            clock: 単調増加する現在時刻を返す関数（締切と同じ基準）。
        """
        self._clock = clock
        self._breaker = CircuitBreaker(clock=clock)
        self._quota = UpstreamQuota(rate_per_second, burst, clock=clock)
        self._concurrency = ConcurrencyLimit(max_concurrent, max_concurrent_per_principal)
        self._flights = SingleFlight()

    async def call[T](
        self,
        method: Callable[..., Awaitable[T]],
        arguments: Mapping[str, Any],
        *,
        principal: str,
        deadline: float,
    ) -> T:
        """方針を通して、上流のメソッドを引数 arguments で呼ぶ。

        同じ主体・同じメソッド・同じ引数の同時の呼び出しは 1 本にまとめる（締切は先に来た呼び出しの
        もの）。主体をキーに含めるので、他の主体の結果は返さない。メソッドが引数の外の状態を読む
        なら、それを引数か主体に含める。

        Args:
            method: 上流を呼ぶメソッド（引数は名前で渡す）。
            arguments: メソッドに渡す引数。値は hashable か list（list は tuple にしてキーにする）。
            principal: 呼び出しの主体。
            deadline: 締切（`clock` と同じ基準の時刻）。

        Returns:
            上流の結果。

        Raises:
            Throttled: 同時実行かクォータの上限に当たったとき。
            UpstreamError: 上流が失敗した・遮断中・締切を越えたとき。
        """

        async def attempt() -> T:
            """クォータを 1 回ぶん使ってから上流を呼ぶ（再試行のたびに呼ばれる）。

            Returns:
                上流の結果。
            """
            self._quota.take()
            return await method(**arguments)

        async def bounded() -> T:
            """同時実行の枠の中で、遮断器・再試行・締切を通して上流を呼ぶ。

            Returns:
                上流の結果。
            """
            with self._concurrency.slot(principal):
                return await call_upstream(self._breaker, attempt, deadline=deadline, clock=self._clock)

        key = (
            principal,
            method,
            frozenset((name, tuple(value) if isinstance(value, list) else value) for name, value in arguments.items()),
        )
        return await self._flights.run(key, bounded)
