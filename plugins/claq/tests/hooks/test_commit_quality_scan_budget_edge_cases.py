"""claq.hooks.commit_quality_scanner の secret scan 実時間予算のテスト（超過時の例外・巨大 1 行・ファイル間で共有する予算の範囲）。

改行の無い巨大 1 行で塞いでいる穴は 2 つ。
(a) `_scan_lint_issues` が `deadline` を受け取らず時間検査を持たないと、`find_file_issues` が
    secret より**先**に呼ぶため、バジェットが原理的に効かない区間ができる。
(b) `_scan_secret_issues` が `index % 2000` でしか確認しないと、行数が 2000 未満のファイル
    （＝巨大 1 行）は index 0 の 1 回、つまり**走査開始前**にしか確認されない。

時刻は `_monotonic` の注入で決定的に与える（実時間に依存させると flaky）。
"""

from __future__ import annotations

import pytest

from claq.hooks import commit_quality_scanner as commit_quality_scanner

# 改行の無い 1MB 級の 1 行。行数は 1 なので「行数」基準では走査開始前の 1 回しか予算確認が起きない。
_HUGE_LINE = "a" * (1024 * 1024)


@pytest.mark.parametrize(
    "scan",
    [
        lambda: commit_quality_scanner._scan_secret_issues("line one", ["line one"], deadline=0.0),
        # 行数 1 でも予算確認が走ること（「行数」基準では素通りする）。
        lambda: commit_quality_scanner._scan_secret_issues(_HUGE_LINE, [_HUGE_LINE], deadline=0.0),
        # lint scanner も同じ 1 本の予算に載っていること。
        lambda: commit_quality_scanner._scan_lint_issues([_HUGE_LINE], deadline=0.0),
    ],
    ids=["secret-short-line", "secret-huge-line", "lint-huge-line"],
)
def test_scanner_raises_when_deadline_passed(monkeypatch: pytest.MonkeyPatch, scan) -> None:
    """渡された deadline を過ぎていれば各 scanner が SecretScanBudgetExceeded を送出すること。"""
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)

    with pytest.raises(commit_quality_scanner.SecretScanBudgetExceeded):
        scan()


@pytest.mark.parametrize(
    "content",
    ["some text", _HUGE_LINE, "api" + "_key" + ' = "abc123"'],  # nosec
    ids=["short", "huge-single-line", "secret-bearing"],
)
def test_lint_budget_exhaustion_is_scan_error_and_skips_secret_scan(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    """find_file_issues 経由の予算超過は fail-closed の scan_error（error）1 件で返り、secret scan は回さないこと。

    予算はフック 1 回で共有の 1 本なので、lint 側で尽きた状態で続行してもホスト側 timeout を
    踏むだけ。通常の lint 例外（warning）と同じ severity へ落とすと、予算切れが commit を
    ブロックしなくなる。secret が実際に走れば検出されるはずの内容でも secret 側は呼ばれない。
    """
    monkeypatch.setattr(commit_quality_scanner, "_monotonic", lambda: 1_000.0)
    monkeypatch.setattr(commit_quality_scanner, "get_staged_file_content", lambda path: content)
    called: list[str] = []
    monkeypatch.setattr(
        commit_quality_scanner, "_scan_secret_issues", lambda *args, **kwargs: called.append("secret") or []
    )

    issues = commit_quality_scanner.find_file_issues("src/app.js", deadline=0.0)

    assert called == []
    assert [(issue["type"], issue["severity"]) for issue in issues] == [("scan_error", "error")]
    assert "lint scan" in issues[0]["message"]
    assert "SecretScanBudgetExceeded" in issues[0]["message"]


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
            """呼び出し回数を数え、常に時刻 0 を返す。"""
            nonlocal reads
            reads += 1
            return 0.0

        monkeypatch.setattr(commit_quality_scanner, "_monotonic", clock)
        commit_quality_scanner._scan_secret_issues("\n".join(lines), lines, deadline=1_000.0)
        return reads

    def test_large_input_samples_clock_repeatedly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """1 ファイルの中で閾値を跨げば繰り返し確認する（「行数」基準との差）。

        改行の無い 1 行でも、パターン数ぶんの走査バイトが積算されるため
        複数回の確認が起きる。
        """
        huge_single_line = "a" * (4 * commit_quality_scanner._SCAN_BUDGET_CHECK_BYTES)

        assert self._count_clock_reads(monkeypatch, [huge_single_line]) > 1

    def test_every_call_re_arms_the_initial_check(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """閾値に満たない入力では開始時の 1 回だけ時刻を読み、呼び出しごとにその 1 回が入ること（＝積算を持ち越さない）。

        これが共有 deadline を効かせている当の性質。積算がファイルを跨いで
        持ち越されるなら、閾値に満たない 2 回目の呼び出しは時刻を 1 度も
        読まず、その回のファイルは deadline 超過を検知できない。
        """
        small = ["short line"] * 50

        first = self._count_clock_reads(monkeypatch, small)
        second = self._count_clock_reads(monkeypatch, small)

        assert (first, second) == (1, 1)
