"""facade/limits.py（上流ごとのクォータ・同時実行の上限・同じ要求の合流と、それらをまとめた上流の方針）のテスト。"""

from __future__ import annotations

import asyncio
import types
from typing import Any

import pytest

from ._support import _Clock


class TestQuota:
    """サーバ自身の絞り（Throttled）と、全主体で共有する上流のクォータ。"""

    def test_throttled_is_429_and_not_retryable(self, facade: types.SimpleNamespace) -> None:
        """Throttled は 429 の UpstreamError だが、再試行の対象にしない（自分の絞りを自分で叩き直さない）。"""
        error = facade.limits.Throttled("upstream quota")
        assert (isinstance(error, facade.upstream.UpstreamError), error.status, error.detail, error.retryable) == (
            True,
            429,
            "upstream quota",
            False,
        )

    def test_quota_allows_burst_then_throttles(self, facade: types.SimpleNamespace) -> None:
        """時間が進まなければ burst 回まで通し、次は待たずに Throttled にする。"""
        quota = facade.limits.UpstreamQuota(1.0, 3, clock=_Clock())
        for _ in range(3):
            quota.take()
        with pytest.raises(facade.limits.Throttled):
            quota.take()

    def test_quota_recovers_after_refill(self, facade: types.SimpleNamespace) -> None:
        """経過時間 × rate のぶん回復し、長く空けても burst を超えて貯めない。"""
        clock = _Clock()
        quota = facade.limits.UpstreamQuota(1.0, 2, clock=clock)
        quota.take()
        quota.take()
        clock.now += 1.0
        quota.take()
        clock.now += 100.0
        quota.take()
        quota.take()
        with pytest.raises(facade.limits.Throttled):
            quota.take()


class TestConcurrencyLimit:
    """上流ごと・主体ごとの同時実行の上限。"""

    def test_upstream_slots_throttle_at_limit(self, facade: types.SimpleNamespace) -> None:
        """主体をまたいだ同時実行の総数が上限に達したら、次の枠は待たずに Throttled にする。"""
        limit = facade.limits.ConcurrencyLimit(2, 2)
        with limit.slot("alice"), limit.slot("bob"), pytest.raises(facade.limits.Throttled) as raised:
            with limit.slot("carol"):
                pass
        assert raised.value.detail == "concurrency limit"

    def test_upstream_slots_recover_after_release(self, facade: types.SimpleNamespace) -> None:
        """枠を返せば、総数の上限で断られていた主体も入れる。"""
        limit = facade.limits.ConcurrencyLimit(1, 1)
        with limit.slot("alice"):
            pass
        with limit.slot("bob"):
            pass

    def test_principal_slots_throttle_at_limit(self, facade: types.SimpleNamespace) -> None:
        """総数に余裕があっても、1 つの主体の同時実行が上限に達したら、その主体の次の枠は Throttled にする。"""
        limit = facade.limits.ConcurrencyLimit(10, 2)
        with limit.slot("alice"), limit.slot("alice"), limit.slot("bob"):
            with pytest.raises(facade.limits.Throttled):
                with limit.slot("alice"):
                    pass

    def test_principal_slots_recover_after_release(self, facade: types.SimpleNamespace) -> None:
        """主体が枠を 1 つ返せば、その主体はまた入れる。"""
        limit = facade.limits.ConcurrencyLimit(10, 2)
        with limit.slot("alice"):
            with limit.slot("alice"):
                pass
            with limit.slot("alice"):
                pass

    def test_principal_slots_forget_idle_principals(self, facade: types.SimpleNamespace) -> None:
        """枠を全て返した主体の記録は消す（主体の数だけ記録が増え続けない）。"""
        limit = facade.limits.ConcurrencyLimit(10, 2)
        for principal in ("alice", "bob", "carol"):
            with limit.slot(principal), limit.slot(principal):
                pass
        assert limit._per_principal == {}

    def test_slot_releases_on_exception(self, facade: types.SimpleNamespace) -> None:
        """枠の中で例外が起きても枠を返す（失敗のたびに枠が減っていかない）。"""
        limit = facade.limits.ConcurrencyLimit(1, 1)
        with pytest.raises(RuntimeError), limit.slot("alice"):
            raise RuntimeError("boom")
        with limit.slot("alice"):
            pass


class _Gate:
    """呼ばれた回数を数え、開けるまで完了しない上流の呼び出し。取り消しと後始末を記録する。"""

    def __init__(self, result: Any = "ok") -> None:
        """返す値（例外なら投げる）を設定する。"""
        self.result = result
        self.calls = 0
        self.events: list[str] = []
        self.opened = asyncio.Event()

    async def __call__(self, **arguments: Any) -> Any:
        """開けられるまで待ち、用意した値を返すか投げる（上流のメソッドとしても呼べるよう引数を受ける）。"""
        self.calls += 1
        try:
            await self.opened.wait()
        except asyncio.CancelledError:
            self.events.append("cancelled")
            raise
        finally:
            self.events.append("closed")
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def _settle() -> None:
    """待っている task を数巡ぶん進める（実時間は待たない）。"""
    for _ in range(5):
        await asyncio.sleep(0)


class TestSingleFlight:
    """同じキーの同時の呼び出しを 1 本にまとめる。"""

    def test_same_key_runs_once(self, facade: types.SimpleNamespace) -> None:
        """同じキーの同時の呼び出しは上流を 1 回だけ呼び、全員が同じ結果を受け取る。"""
        flights = facade.limits.SingleFlight()
        gate = _Gate()

        async def scenario() -> list[Any]:
            """同じキーで 3 本を同時に走らせ、揃ってから上流を完了させる。"""
            waiters = [asyncio.ensure_future(flights.run("k", gate)) for _ in range(3)]
            await _settle()
            gate.opened.set()
            return await asyncio.gather(*waiters)

        assert (asyncio.run(scenario()), gate.calls) == (["ok", "ok", "ok"], 1)

    def test_different_keys_do_not_merge(self, facade: types.SimpleNamespace) -> None:
        """キーが違えば、同時でもそれぞれ上流を呼ぶ。"""
        flights = facade.limits.SingleFlight()
        gate = _Gate()

        async def scenario() -> list[Any]:
            """違うキーで 2 本を同時に走らせる。"""
            waiters = [asyncio.ensure_future(flights.run(key, gate)) for key in ("a", "b")]
            await _settle()
            gate.opened.set()
            return await asyncio.gather(*waiters)

        assert (asyncio.run(scenario()), gate.calls) == (["ok", "ok"], 2)

    def test_key_released_after_success_and_after_failure(self, facade: types.SimpleNamespace) -> None:
        """終わった呼び出し（成功でも失敗でも）の直後に同じキーで呼べば、まとめずに上流を呼び直す。"""
        flights = facade.limits.SingleFlight()
        error = facade.upstream.UpstreamError(503)
        outcomes: list[Any] = [error, "ok"]
        calls: list[int] = []

        async def operation() -> Any:
            """用意した順に、例外なら投げ、それ以外なら返す。"""
            calls.append(1)
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        async def scenario() -> list[Any]:
            """失敗・成功・成功の直後、の順に同じキーで呼ぶ。"""
            seen: list[Any] = []
            with pytest.raises(facade.upstream.UpstreamError) as raised:
                await flights.run("k", operation)
            seen.append(raised.value)
            seen.append(await flights.run("k", operation))
            outcomes.append("again")
            seen.append(await flights.run("k", operation))
            return seen

        assert (asyncio.run(scenario()), len(calls)) == ([error, "ok", "again"], 3)

    def test_joiner_cancellation_keeps_shared_call(self, facade: types.SimpleNamespace) -> None:
        """待ち手の一部が取り消されても、残りの待ち手のために共有の呼び出しは続ける。"""
        flights = facade.limits.SingleFlight()
        gate = _Gate()

        async def scenario() -> tuple[bool, Any]:
            """2 本のうち合流した側を取り消し、もう 1 本が結果を受け取れるかを見る。"""
            first = asyncio.ensure_future(flights.run("k", gate))
            await _settle()
            joiner = asyncio.ensure_future(flights.run("k", gate))
            await _settle()
            joiner.cancel()
            await _settle()
            gate.opened.set()
            return joiner.cancelled(), await first

        assert (asyncio.run(scenario()), gate.calls, gate.events) == ((True, "ok"), 1, ["closed"])

    def test_last_waiter_cancellation_cancels_and_waits(self, facade: types.SimpleNamespace) -> None:
        """最後の待ち手が取り消されたら、共有の呼び出しを取り消し、後始末が終わってから取り消しを伝える。"""
        flights = facade.limits.SingleFlight()
        gate = _Gate()
        observed: list[list[str]] = []

        async def waiter() -> Any:
            """共有の呼び出しを待ち、取り消しが届いた時点の上流の記録を写す。"""
            try:
                return await flights.run("k", gate)
            except asyncio.CancelledError:
                observed.append(list(gate.events))
                raise

        async def scenario() -> list[bool]:
            """2 本とも取り消し、どちらも取り消しで終わるかを見る。"""
            waiters = [asyncio.ensure_future(waiter()) for _ in range(2)]
            await _settle()
            for task in waiters:
                task.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            return [task.cancelled() for task in waiters]

        assert (asyncio.run(scenario()), observed[-1], gate.events) == (
            [True, True],
            ["cancelled", "closed"],
            ["cancelled", "closed"],
        )

    def test_new_call_during_cancelled_cleanup_calls_upstream_again(self, facade: types.SimpleNamespace) -> None:
        """最後の待ち手の取り消し後、上流の後始末が終わる前に来た同じキーの呼び出しは、取り消し中の呼び出しに
        合流せず上流を呼び直して結果を受け取る。古い呼び出しが止まっても新しい呼び出しの登録は消えず、その後に
        来た呼び出しも新しい呼び出しに合流する。"""
        flights = facade.limits.SingleFlight()
        cleanup = asyncio.Event()
        release = asyncio.Event()
        calls: list[int] = []

        async def operation() -> str:
            """1 回目は取り消されるまで待ち、取り消し後の後始末で await を挟んでから止まる。2 回目は開けられたら返す。"""
            calls.append(1)
            if len(calls) == 1:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    await cleanup.wait()
                    raise
            await release.wait()
            return "fresh"

        async def scenario() -> tuple[bool, list[str]]:
            """1 本目を取り消して後始末の途中で 2 本目を出し、古い後始末を終えてから 3 本目を出して完了させる。"""
            first = asyncio.ensure_future(flights.run("k", operation))
            await _settle()
            first.cancel()
            await _settle()
            second = asyncio.ensure_future(flights.run("k", operation))
            await _settle()
            cleanup.set()
            await _settle()
            third = asyncio.ensure_future(flights.run("k", operation))
            await _settle()
            release.set()
            return first.cancelled(), await asyncio.gather(second, third)

        assert (asyncio.run(scenario()), len(calls), flights._flights) == ((True, ["fresh", "fresh"]), 2, {})

    def test_cancellation_racing_completion_releases_key(self, facade: types.SimpleNamespace) -> None:
        """共有の呼び出しが終わるのと同時に最後の待ち手が取り消されても、取り消しを伝えてキーを残さない。"""
        flights = facade.limits.SingleFlight()
        waiters: list[asyncio.Task[Any]] = []

        async def operation() -> str:
            """待ち手を取り消してから、そのまま返す（完了と取り消しを同じ巡に重ねる）。"""
            waiters[0].cancel()
            return "ok"

        async def scenario() -> bool:
            """1 本だけ走らせ、取り消しで終わるかを見る。"""
            waiters.append(asyncio.ensure_future(flights.run("k", operation)))
            await asyncio.gather(*waiters, return_exceptions=True)
            return waiters[0].cancelled()

        assert (asyncio.run(scenario()), flights._flights) == (True, {})


def _scripted(outcomes: list[Any]) -> Any:
    """用意した順に、例外なら投げ、それ以外なら返す上流のメソッドを作る。"""

    async def method(**arguments: Any) -> Any:
        """次の結果を返すか投げる。"""
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    return method


def _policy(facade: types.SimpleNamespace, clock: _Clock, **limits: Any) -> Any:
    """時計 clock の上で、上限を limits で上書きした UpstreamPolicy を作る（既定はどの上限にも当たらない値）。"""
    settings = {"rate_per_second": 1.0, "burst": 100, "max_concurrent": 100, "max_concurrent_per_principal": 100}
    return facade.limits.UpstreamPolicy(**{**settings, **limits}, clock=clock)


async def _outcome(facade: types.SimpleNamespace, call: Any) -> Any:
    """呼び出しの結果を返す。UpstreamError なら、投げずにその例外を返す（結果と並べて比べるため）。"""
    try:
        return await call
    except facade.upstream.UpstreamError as error:
        return error


class TestUpstreamPolicy:
    """上流ごとの方針（合流 → 同時実行の上限 → 遮断器・再試行・締切 → 試行ごとのクォータ）。"""

    def test_policy_merges_same_principal_and_arguments(self, facade: types.SimpleNamespace) -> None:
        """同じ主体・同じメソッド・同じ引数（list の値を含む）の同時の呼び出しは、上流を 1 回だけ呼ぶ。"""
        clock = _Clock()
        policy = _policy(facade, clock)
        gate = _Gate()
        arguments = {"status_codes": [1, 2], "page": None}

        async def scenario() -> list[Any]:
            """同じ要求を 2 本同時に走らせる。"""
            calls = [
                asyncio.ensure_future(policy.call(gate, dict(arguments), principal="alice", deadline=clock.now + 10))
                for _ in range(2)
            ]
            await _settle()
            gate.opened.set()
            return await asyncio.gather(*calls)

        assert (asyncio.run(scenario()), gate.calls) == (["ok", "ok"], 1)

    def test_policy_does_not_merge_across_principals_or_arguments(self, facade: types.SimpleNamespace) -> None:
        """主体か引数が違えば、同時でもまとめない（他の主体の結果を返さない）。"""
        clock = _Clock()
        policy = _policy(facade, clock)
        gate = _Gate()
        requests = [("alice", {"order_id": "a"}), ("bob", {"order_id": "a"}), ("alice", {"order_id": "b"})]

        async def scenario() -> list[Any]:
            """主体か引数の違う 3 本を同時に走らせる。"""
            calls = [
                asyncio.ensure_future(policy.call(gate, arguments, principal=principal, deadline=clock.now + 10))
                for principal, arguments in requests
            ]
            await _settle()
            gate.opened.set()
            return await asyncio.gather(*calls)

        assert (asyncio.run(scenario()), gate.calls) == (["ok", "ok", "ok"], 3)

    def test_policy_quota_is_shared_across_principals(self, facade: types.SimpleNamespace) -> None:
        """上流のクォータは主体をまたいで数え、使い切ったら別の主体も絞り、時間が経てば回復する。"""
        clock = _Clock()
        policy = _policy(facade, clock, burst=2)
        method = _scripted(["a", "b", "c"])

        async def scenario() -> list[Any]:
            """3 つの主体が順に呼び、回復を待って 3 つめの主体が呼び直す。"""
            seen = [
                await _outcome(facade, policy.call(method, {}, principal=principal, deadline=clock.now + 10))
                for principal in ("alice", "bob", "carol")
            ]
            clock.now += 1.0
            seen.append(await policy.call(method, {}, principal="carol", deadline=clock.now + 10))
            return seen

        first, second, throttled, recovered = asyncio.run(scenario())
        assert (first, second, type(throttled), throttled.detail, recovered) == (
            "a",
            "b",
            facade.limits.Throttled,
            "upstream quota",
            "c",
        )

    def test_policy_quota_counts_each_attempt(self, facade: types.SimpleNamespace) -> None:
        """再試行も上流を 1 回呼ぶぶんとして数える（1 回の呼び出しの再試行で 2 回ぶんを使う）。"""
        clock = _Clock()
        policy = _policy(facade, clock, burst=2)
        method = _scripted([facade.upstream.UpstreamError(503, retry_after=0.0), "ok", "unused"])

        async def scenario() -> list[Any]:
            """再試行を挟んで成功する呼び出しの後に、もう 1 回呼ぶ。"""
            return [
                await _outcome(facade, policy.call(method, {}, principal="alice", deadline=clock.now + 10)) for _ in range(2)
            ]

        succeeded, throttled = asyncio.run(scenario())
        assert (succeeded, type(throttled)) == ("ok", facade.limits.Throttled)

    def test_policy_throttle_does_not_open_breaker(self, facade: types.SimpleNamespace) -> None:
        """同時実行とクォータで絞った回数が遮断器の閾値を超えても、遮断器は開かない（上流は健全なまま呼べる）。

        遮断器は既定（連続 5 回で遮断し、30 秒で試しに通す）。時計は 30 秒より短くしか進めない。
        """
        clock = _Clock()
        policy = _policy(facade, clock, burst=1, max_concurrent=1, max_concurrent_per_principal=1)
        gate = _Gate()

        async def scenario() -> tuple[list[str], Any]:
            """1 本で枠を塞いで同時実行で 5 回絞り、それを終えてクォータで 5 回絞り、回復を待って呼び直す。"""
            holder = asyncio.ensure_future(policy.call(gate, {}, principal="alice", deadline=clock.now + 10))
            await _settle()
            throttled = [
                await _outcome(facade, policy.call(gate, {"n": n}, principal="bob", deadline=clock.now + 10)) for n in range(5)
            ]
            gate.opened.set()
            await holder
            throttled += [
                await _outcome(facade, policy.call(gate, {"n": n}, principal="bob", deadline=clock.now + 10)) for n in range(5)
            ]
            clock.now += 1.0
            return [error.detail for error in throttled], await policy.call(gate, {}, principal="bob", deadline=clock.now + 10)

        assert asyncio.run(scenario()) == (["concurrency limit"] * 5 + ["upstream quota"] * 5, "ok")

    def test_policy_cancellation_reaches_upstream(self, facade: types.SimpleNamespace) -> None:
        """呼び出し元が取り消されたら、上流の呼び出しも取り消し、その後始末が走る。"""
        clock = _Clock()
        policy = _policy(facade, clock)
        gate = _Gate()

        async def scenario() -> bool:
            """上流が終わる前に呼び出し元を取り消す。"""
            call = asyncio.ensure_future(policy.call(gate, {}, principal="alice", deadline=clock.now + 10))
            await _settle()
            call.cancel()
            await asyncio.gather(call, return_exceptions=True)
            return call.cancelled()

        assert (asyncio.run(scenario()), gate.events) == (True, ["cancelled", "closed"])
