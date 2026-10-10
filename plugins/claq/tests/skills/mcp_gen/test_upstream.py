"""facade/upstream.py（上流の呼び出しを包む部品と、締切付きの上流呼び出し）のテスト。"""

from __future__ import annotations

import asyncio
import math
import types
from typing import Any

import pytest

from ._support import _Clock


class _Sleeper:
    """待ち時間を記録するだけの sleep。"""

    def __init__(self) -> None:
        """記録を空にする。"""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """待たずに記録する。"""
        self.delays.append(delay)


class TestUpstream:
    """上流の呼び出しを包む部品。"""

    def test_retryable_and_message(self, facade: types.SimpleNamespace) -> None:
        """一時的な失敗の判定と、モデル向けの文面（内部の詳細を含めない）。"""
        error = facade.upstream.UpstreamError
        assert error(None).retryable and error(429).retryable and error(503).retryable
        assert not error(404).retryable
        assert "status=500" in str(error(500, "boom"))
        expected = {
            400: "引数",
            401: "管理者",
            403: "見つからないか",
            404: "見つからないか",
            409: "取得し直して",
            412: "取得し直して",
            429: "混み合って",
            500: "一時的",
            None: "一時的",
        }
        for status, phrase in expected.items():
            message = facade.upstream.model_message(error(status, "secret internal detail"))
            assert phrase in message
            assert "secret" not in message

    def test_forbidden_reads_like_not_found(self, facade: types.SimpleNamespace) -> None:
        """403 は 404 と同じ文面にする（権限が無いことから、存在を漏らさない）。"""
        message = facade.upstream.model_message
        error = facade.upstream.UpstreamError
        assert message(error(403)) == message(error(404))

    def test_detail_is_safe_to_log(self, facade: types.SimpleNamespace) -> None:
        """detail は制御文字と CR/LF を除き、200 文字に切ってから持つ（例外の文面も同じ）。"""
        error = facade.upstream.UpstreamError(502, "bad\r\ngateway\x1b[2J\x00" + "x" * 300)
        assert error.detail == ("badgateway[2J" + "x" * 300)[:200]
        assert "\n" not in str(error) and "\r" not in str(error) and "\x1b" not in str(error)
        assert str(error) == f"upstream status=502 {error.detail}"

    def test_retry_until_success(self, facade: types.SimpleNamespace) -> None:
        """一時的な失敗は指数バックオフ（ジッタ込み）で再試行する。"""
        outcomes = [facade.upstream.UpstreamError(503), facade.upstream.UpstreamError(None), "ok"]
        sleeper = _Sleeper()

        async def operation() -> str:
            """用意した順に失敗と成功を返す。"""
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        result = asyncio.run(
            facade.upstream.call_with_retry(operation, base_delay=0.2, deadline=math.inf, sleep=sleeper, jitter=lambda: 1.0)
        )
        assert result == "ok"
        assert sleeper.delays == [0.2, 0.4]

    def test_retry_gives_up(self, facade: types.SimpleNamespace) -> None:
        """再試行しない失敗・回数切れ・締切越えでは諦める。上流の retry_after に従う。"""
        error = facade.upstream.UpstreamError

        def failing(exc: Exception) -> Any:
            """常に同じ失敗を投げる関数を作る。"""

            async def operation() -> None:
                """失敗を投げる。"""
                raise exc

            return operation

        sleeper = _Sleeper()
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(error(404)), deadline=math.inf, sleep=sleeper))
        assert sleeper.delays == []
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(error(429, retry_after=1.5)), attempts=2, deadline=math.inf, sleep=sleeper))
        assert sleeper.delays == [1.5]
        clock = _Clock(100.0)
        with pytest.raises(error):
            asyncio.run(
                facade.upstream.call_with_retry(failing(error(503)), deadline=100.1, clock=clock, sleep=sleeper)
            )
        assert sleeper.delays == [1.5]

    def test_retry_after_is_capped(self, facade: types.SimpleNamespace) -> None:
        """上流の retry_after が上限（既定 5 秒）を超えるなら、待たずに失敗を投げる。"""
        error = facade.upstream.UpstreamError
        sleeper = _Sleeper()

        def failing(retry_after: float) -> Any:
            """retry_after 付きの 429 を投げ続ける関数を作る。"""

            async def operation() -> None:
                """429 を投げる。"""
                raise error(429, retry_after=retry_after)

            return operation

        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(5.1), deadline=math.inf, sleep=sleeper))
        assert sleeper.delays == []
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(5.0), attempts=2, deadline=math.inf, sleep=sleeper))
        assert sleeper.delays == [5.0]
        with pytest.raises(error):
            asyncio.run(facade.upstream.call_with_retry(failing(1.0), max_retry_after=0.5, deadline=math.inf, sleep=sleeper))
        assert sleeper.delays == [5.0]

    def test_circuit_breaker(self, facade: types.SimpleNamespace) -> None:
        """連続した一時的な失敗で遮断し、時間が経てば試しに通し、成功で戻る。"""
        error = facade.upstream.UpstreamError
        clock = _Clock()
        breaker = facade.upstream.CircuitBreaker(failure_threshold=2, reset_seconds=30, clock=clock)

        async def fail(status: int) -> None:
            """指定のステータスで失敗する。"""
            raise error(status)

        async def succeed() -> str:
            """成功する。"""
            return "ok"

        for status in (404, 404, 503, 503):
            with pytest.raises(error):
                asyncio.run(breaker.call(lambda status=status: fail(status)))
        with pytest.raises(error, match="circuit open"):
            asyncio.run(breaker.call(succeed))
        clock.now += 30
        assert asyncio.run(breaker.call(succeed)) == "ok"

    def test_fan_out(self, facade: types.SimpleNamespace) -> None:
        """成功・失敗・締切超えを分けて返し、全体は失敗にしない。"""
        error = facade.upstream.UpstreamError

        async def ok() -> int:
            """成功する。"""
            return 1

        async def broken() -> None:
            """上流の失敗を投げる。"""
            raise error(503)

        async def slow() -> None:
            """締切に間に合わない。"""
            await asyncio.sleep(10)

        results, failures = asyncio.run(
            facade.upstream.fan_out({"a": ok, "b": broken, "c": slow}, timeout=0.05)
        )
        assert results == {"a": 1}
        assert failures["b"].status == 503
        assert failures["c"].status is None

    def test_fan_out_cancellation_stops_unfinished_calls(self, facade: types.SimpleNamespace) -> None:
        """fan_out を待っている間に呼び出し元が取り消されたら、終わっていない上流の呼び出しも取り消してから終える。"""
        observed: list[str] = []

        async def scenario() -> list[str]:
            """遅い呼び出しが始まった後で fan_out を取り消し、取り消しが終わった時点の記録を返す。"""
            started = asyncio.Event()

            async def slow() -> None:
                """取り消されるまで待つ。"""
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    observed.append("cancelled")
                    raise

            task = asyncio.ensure_future(facade.upstream.fan_out({"a": slow}, timeout=10))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            return list(observed)

        assert asyncio.run(scenario()) == ["cancelled"]

    def test_fan_out_reraises_bugs(self, facade: types.SimpleNamespace) -> None:
        """上流の失敗以外の例外（プログラムの誤り）は握りつぶさない。"""

        async def bug() -> None:
            """誤りを投げる。"""
            raise KeyError("bug")

        with pytest.raises(KeyError):
            asyncio.run(facade.upstream.fan_out({"x": bug}, timeout=1))


def _breaker_is_closed(facade: types.SimpleNamespace, breaker: Any) -> bool:
    """遮断器が上流を呼ぶか（遮断中なら呼ばずに "circuit open" で失敗する）。"""

    async def succeed() -> str:
        """成功する。"""
        return "ok"

    try:
        return asyncio.run(breaker.call(succeed)) == "ok"
    except facade.upstream.UpstreamError as error:
        assert error.detail == "circuit open"
        return False


def _fail_once(facade: types.SimpleNamespace, breaker: Any) -> None:
    """遮断器を通して、一時的な失敗（503）を 1 回起こす。"""

    async def unavailable() -> None:
        """一時的に失敗する。"""
        raise facade.upstream.UpstreamError(503)

    with pytest.raises(facade.upstream.UpstreamError):
        asyncio.run(breaker.call(unavailable))


class TestCallUpstream:
    """締切付きの上流呼び出し（遮断器の内側で、再試行を含む全体を締切で打ち切る）。

    テスト契約:
    - 対象: `facade.upstream.call_upstream(breaker, operation, *, deadline, clock)`
    - 結果: 成功は operation の値。締切超えは `UpstreamError(None, "deadline exceeded")` で、遮断器が
      失敗として数える。再試行しない失敗はそのまま投げ、遮断器は数えない
    - 許容時間: 各テスト 1 秒未満（実時間の待ちは `asyncio.sleep(0)` だけ）
    - 後始末: 締切超えでは operation を取り消し、operation の finally が走る
    - モックの境界: 時計と operation を偽物にする。イベントループ・`asyncio.timeout`・
      `CircuitBreaker`・`call_with_retry` は実物
    """

    def test_call_upstream_success_returns_result(self, facade: types.SimpleNamespace) -> None:
        """締切までに終われば operation の結果を返し、遮断器に成功を記録する（それまでの連続失敗を戻す）。"""
        breaker = facade.upstream.CircuitBreaker(failure_threshold=2)
        _fail_once(facade, breaker)

        async def operation() -> str:
            """成功する。"""
            return "ok"

        result = asyncio.run(facade.upstream.call_upstream(breaker, operation, deadline=10.0, clock=_Clock(0.0)))
        _fail_once(facade, breaker)
        assert (result, _breaker_is_closed(facade, breaker)) == ("ok", True)

    def test_call_upstream_non_retryable_passes_through(self, facade: types.SimpleNamespace) -> None:
        """再試行しない失敗（404）は、そのまま投げ、遮断器の失敗として数えない。"""
        error = facade.upstream.UpstreamError(404, "missing")
        breaker = facade.upstream.CircuitBreaker(failure_threshold=1)

        async def operation() -> None:
            """見つからない。"""
            raise error

        with pytest.raises(facade.upstream.UpstreamError) as raised:
            asyncio.run(facade.upstream.call_upstream(breaker, operation, deadline=10.0, clock=_Clock(0.0)))
        assert (raised.value is error, _breaker_is_closed(facade, breaker)) == (True, True)

    def test_call_upstream_retries_on_given_clock(self, facade: types.SimpleNamespace) -> None:
        """再試行の締切は渡した時計で測る（実時間の時計では締切を過ぎていても、渡した時計で間に合えば再試行する）。"""
        outcomes: list[Any] = [facade.upstream.UpstreamError(503, retry_after=0.0), "ok"]

        async def operation() -> str:
            """1 回目は一時的に失敗し、2 回目は成功する。"""
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        breaker = facade.upstream.CircuitBreaker()
        assert asyncio.run(facade.upstream.call_upstream(breaker, operation, deadline=-1e9, clock=_Clock(-2e9))) == "ok"

    def test_call_upstream_deadline_exceeded_counts_as_failure(self, facade: types.SimpleNamespace) -> None:
        """締切を過ぎたら operation の完了を待たずに UpstreamError(None, "deadline exceeded") にし、遮断器が失敗として数える。"""
        breaker = facade.upstream.CircuitBreaker(failure_threshold=1)

        async def operation() -> str:
            """1 回だけ制御を返してから終わる（締切を過ぎていれば、その間に取り消される）。"""
            await asyncio.sleep(0)
            return "late"

        with pytest.raises(facade.upstream.UpstreamError) as exceeded:
            asyncio.run(facade.upstream.call_upstream(breaker, operation, deadline=50.0, clock=_Clock(100.0)))
        assert (exceeded.value.status, exceeded.value.detail) == (None, "deadline exceeded")
        assert _breaker_is_closed(facade, breaker) is False

    def test_call_upstream_deadline_cancels_operation(self, facade: types.SimpleNamespace) -> None:
        """締切を過ぎたら operation を取り消し、operation の後始末（finally）を走らせる。"""
        events: list[str] = []

        async def operation() -> str:
            """取り消しと後始末を記録する。"""
            try:
                await asyncio.sleep(0)
                return "late"
            except asyncio.CancelledError:
                events.append("cancelled")
                raise
            finally:
                events.append("closed")

        with pytest.raises(facade.upstream.UpstreamError):
            asyncio.run(facade.upstream.call_upstream(facade.upstream.CircuitBreaker(), operation, deadline=50.0, clock=_Clock(100.0)))
        assert events == ["cancelled", "closed"]
