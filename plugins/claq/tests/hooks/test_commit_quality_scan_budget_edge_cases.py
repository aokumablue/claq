"""claq.hooks.commit_quality_scanner の secret scan 実時間予算のテスト（超過時の例外・巨大 1 行・ファイル間で共有する予算の範囲）。"""

from __future__ import annotations

from unittest import mock

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner

from ._support import _scan_deadline


def test_scan_secret_issues_raises_on_time_budget_exceeded(monkeypatch: pytest.MonkeyPatch) -> None:
    """渡された deadline を過ぎていれば例外として送出され、
    find_file_issues 側で scan_error（severity error、fail-closed）になること。"""
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)

    with pytest.raises(commit_quality_scanner.SecretScanBudgetExceeded):
        commit_quality_scanner._scan_secret_issues("line one", ["line one"], deadline=0.0)


def test_find_file_issues_secret_scan_budget_exceeded_is_scan_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_file_issues 経由でも時間バジェット超過は scan_error（error）として報告される。"""
    calls = iter([0.0, 1_000.0])
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: next(calls))
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: "some text")

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

    scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
    assert len(scan_errors) == 1
    assert scan_errors[0]["severity"] == "error"


class TestScanBudgetCoversHugeSingleLine:
    """実時間バジェットが「改行の無い巨大 1 行」にも効くこと。

    塞いでいる穴は 2 つ。
    (a) `_scan_lint_issues` が `deadline` を受け取らず時間検査を持たないと、
        `find_file_issues` が secret より**先**に呼ぶため、バジェットが
        原理的に効かない区間ができる。
    (b) `_scan_secret_issues` が `index % 2000` でしか確認しないと、
        行数が 2000 未満のファイル（＝巨大 1 行）は index 0 の 1 回、つまり
        **走査開始前**にしか確認されない。

    時刻は `_monotonic` の注入で決定的に与える（実時間に依存させると flaky）。
    """

    # 改行の無い 1MB 級の 1 行。行数は 1 なので「行数」基準では
    # 走査開始前の 1 回しか予算確認が起きない。
    _HUGE_LINE = "a" * (1024 * 1024)

    def test_lint_scan_raises_when_budget_already_exhausted(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lint scanner も同じ 1 本の予算に載っていること。"""
        monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)

        with pytest.raises(commit_quality_scanner.SecretScanBudgetExceeded):
            commit_quality_scanner._scan_lint_issues([self._HUGE_LINE], deadline=0.0)

    def test_secret_scan_raises_on_huge_single_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """行数 1 でも予算確認が走ること（「行数」基準では素通りする）。"""
        monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)

        with pytest.raises(commit_quality_scanner.SecretScanBudgetExceeded):
            commit_quality_scanner._scan_secret_issues(
                self._HUGE_LINE, [self._HUGE_LINE], deadline=0.0
            )

    def test_find_file_issues_returns_scan_error_within_budget(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """巨大 1 行でも deadline が効き、fail-closed の scan_error で戻ること。"""
        monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)
        monkeypatch.setattr(
            commit_quality_scanner, "get_staged_file_content", lambda path: self._HUGE_LINE
        )

        issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=0.0)

        scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
        assert len(scan_errors) == 1
        assert scan_errors[0]["severity"] == "error"
        assert "SecretScanBudgetExceeded" in scan_errors[0]["message"]

    def test_lint_budget_exhaustion_skips_secret_scan(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """lint 側で予算が尽きたら secret scan は回さず error で返すこと。

        予算はフック 1 回で共有の 1 本なので、尽きた状態で続行しても
        ホスト側 timeout を踏むだけ。通常の lint 例外（warning）と同じ
        severity へ落とすと、予算切れが commit をブロックしなくなる。
        """
        monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)
        # secret が実際に走れば検出されるはずの内容を置く。
        content = "api" + "_key" + ' = "abc123"'  # nosec
        monkeypatch.setattr(
            commit_quality_scanner, "get_staged_file_content", lambda path: content
        )
        called: list[str] = []
        monkeypatch.setattr(
            commit_quality_scanner,
            "_scan_secret_issues",
            lambda *args, **kwargs: called.append("secret") or [],
        )

        issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=0.0)

        assert called == []
        assert [issue["severity"] for issue in issues] == ["error"]
        assert "lint scan" in issues[0]["message"]

    def test_ordinary_lint_exception_stays_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """予算超過**以外**の lint 例外は warning のままであること。"""
        content = "api" + "_key" + ' = "abc123"'  # nosec
        monkeypatch.setattr(
            commit_quality_scanner, "get_staged_file_content", lambda path: content
        )
        monkeypatch.setattr(
            commit_quality_scanner,
            "_scan_lint_issues",
            mock.Mock(side_effect=ValueError("boom")),
        )

        issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=_scan_deadline())

        scan_errors = [issue for issue in issues if issue["type"] == "scan_error"]
        assert [issue["severity"] for issue in scan_errors] == ["warning"]
        # secret 側は生存している（早期 return していない）。
        assert any(issue["type"] == "secret" for issue in issues)


class TestScanBudgetAccumulatorScope:
    """`_ScanBudget` の積算はインスタンス単位、deadline だけが共有であること。

    共有されるのは `deadline` で、走査済みバイト数の積算はスキャナ呼び出し
    ごとにリセットされる。これは仕様であり、実時間の上界を与えているのが
    積算間隔ではなく共有 deadline の方だから成立する。ここを取り違えると
    「`_SCAN_BUDGET_CHECK_BYTES` がファイルを跨いで効く」と誤読される。

    既存の共有予算テストは fake clock が 1 サンプルあたり 9 秒進むため、
    積算がどう振る舞っても最初のサンプルで deadline を割ってしまい、この
    差を判別できない。ここでは時刻を進めない clock で「何回読まれたか」
    だけを数えて固定する。
    """

    def _count_clock_reads(self, monkeypatch: pytest.MonkeyPatch, lines: list[str]) -> int:
        """deadline に余裕がある状態で `_monotonic()` の呼び出し回数を数える。"""
        reads = 0

        def clock() -> float:
            nonlocal reads
            reads += 1
            return 0.0

        monkeypatch.setattr(commit_quality_scanner, "_monotonic", clock)
        commit_quality_scanner._scan_secret_issues(
            "\n".join(lines), lines, deadline=1_000.0
        )
        return reads

    def test_small_input_samples_clock_once(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """閾値に満たない入力では走査開始時の 1 回だけ時刻を読む。"""
        assert self._count_clock_reads(monkeypatch, ["short line"] * 50) == 1

    def test_large_input_samples_clock_repeatedly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """1 ファイルの中で閾値を跨げば繰り返し確認する（「行数」基準との差）。

        改行の無い 1 行でも、パターン数ぶんの走査バイトが積算されるため
        複数回の確認が起きる。
        """
        huge_single_line = "a" * (4 * commit_quality_scanner._SCAN_BUDGET_CHECK_BYTES)

        reads = self._count_clock_reads(monkeypatch, [huge_single_line])

        assert reads > 1

    def test_every_call_re_arms_the_initial_check(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """呼び出しごとに必ず開始時の 1 回が入ること（＝積算を持ち越さない）。

        これが共有 deadline を効かせている当の性質。積算がファイルを跨いで
        持ち越されるなら、閾値に満たない 2 回目の呼び出しは時刻を 1 度も
        読まず、その回のファイルは deadline 超過を検知できない。
        """
        small = ["short line"] * 50

        first = self._count_clock_reads(monkeypatch, small)
        second = self._count_clock_reads(monkeypatch, small)

        assert (first, second) == (1, 1)
