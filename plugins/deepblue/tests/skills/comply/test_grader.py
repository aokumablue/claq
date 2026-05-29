"""grader モジュールのテスト — LLM分類によるコンプライアンス採点。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from devgear.skills.comply.grader import ComplianceResult, grade
from devgear.skills.comply.parser import ComplianceSpec, ObservationEvent, parse_spec, parse_trace

FIXTURES = (
    Path(__file__).resolve().parents[3] / "src" / "devgear" / "skills" / "comply" / "fixtures"
)


@pytest.fixture
def tdd_spec():
    return parse_spec(FIXTURES / "tdd_spec.yaml")


@pytest.fixture
def compliant_trace():
    return parse_trace(FIXTURES / "compliant_trace.jsonl")


@pytest.fixture
def noncompliant_trace():
    return parse_trace(FIXTURES / "noncompliant_trace.jsonl")


def _mock_compliant_classification(spec, trace, model="haiku"):  # noqa: ARG001
    """LLMが準拠トレースを正しく分類したケースを模擬する。"""
    return {
        "write_test": [0],
        "run_test_red": [1],
        "write_impl": [2],
        "run_test_green": [3],
        "refactor": [4],
    }


def _mock_noncompliant_classification(spec, trace, model="haiku"):
    """LLMが非準拠トレース（テスト前に実装）を分類したケースを模擬する。"""
    return {
        "write_impl": [0],  # src/fib.py が先に書かれる
        "write_test": [1],  # テストが次に書かれる
        "run_test_green": [2],  # 成功したテスト実行のみ
    }


def _mock_empty_classification(spec, trace, model="haiku"):
    return {}


class TestGradeCompliant:
    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_returns_compliance_result(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        assert isinstance(result, ComplianceResult)

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_full_compliance(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        assert result.compliance_rate == 1.0

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_all_required_steps_detected(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        required_results = [
            s for s in result.steps if s.step_id in ("write_test", "run_test_red", "write_impl", "run_test_green")
        ]
        assert all(s.detected for s in required_results)

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_optional_step_detected(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        refactor = next(s for s in result.steps if s.step_id == "refactor")
        assert refactor.detected is True

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_no_hook_promotion_recommended(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        assert result.recommend_hook_promotion is False

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_step_evidence_not_empty(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        for step in result.steps:
            if step.detected:
                assert len(step.evidence) > 0


class TestGradeNoncompliant:
    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_noncompliant_classification)
    def test_low_compliance(self, mock_cls, tdd_spec, noncompliant_trace) -> None:
        result = grade(tdd_spec, noncompliant_trace)
        assert result.compliance_rate < 1.0

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_noncompliant_classification)
    def test_write_test_fails_ordering(self, mock_cls, tdd_spec, noncompliant_trace) -> None:
        """write_test は before_step=write_impl だが、実装後にテストが書かれている。"""
        result = grade(tdd_spec, noncompliant_trace)
        write_test = next(s for s in result.steps if s.step_id == "write_test")
        assert write_test.detected is False

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_noncompliant_classification)
    def test_run_test_red_not_detected(self, mock_cls, tdd_spec, noncompliant_trace) -> None:
        result = grade(tdd_spec, noncompliant_trace)
        run_red = next(s for s in result.steps if s.step_id == "run_test_red")
        assert run_red.detected is False

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_noncompliant_classification)
    def test_hook_promotion_recommended(self, mock_cls, tdd_spec, noncompliant_trace) -> None:
        result = grade(tdd_spec, noncompliant_trace)
        assert result.recommend_hook_promotion is True

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_noncompliant_classification)
    def test_failure_reasons_present(self, mock_cls, tdd_spec, noncompliant_trace) -> None:
        result = grade(tdd_spec, noncompliant_trace)
        failed_steps = [s for s in result.steps if not s.detected and s.step_id != "refactor"]
        for step in failed_steps:
            assert step.failure_reason is not None


class TestGradeEdgeCases:
    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_empty_classification)
    def test_empty_trace(self, mock_cls, tdd_spec) -> None:
        result = grade(tdd_spec, [])
        assert result.compliance_rate == 0.0
        assert result.recommend_hook_promotion is True

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_compliance_rate_is_ratio_of_required_only(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        assert result.compliance_rate == 1.0

    @patch("devgear.skills.comply.grader.classify_events", side_effect=_mock_compliant_classification)
    def test_spec_id_in_result(self, mock_cls, tdd_spec, compliant_trace) -> None:
        result = grade(tdd_spec, compliant_trace)
        assert result.spec_id == "tdd-workflow"


class TestTemporalOrderAfterStepViolation:
    """_check_temporal_order: after_step 制約違反（行 41）のテスト。

    デシジョンテーブル:
      after_step あり / after_events あり / event.timestamp <= latest_after → 違反で失敗
      after_step あり / after_events あり / event.timestamp >  latest_after → 成功
      after_step あり / after_events なし                                   → "not yet detected" で失敗
    """

    def _make_spec_with_after(self) -> ComplianceSpec:
        """step_b が after_step=step_a を持つ最小仕様を返す。"""
        from devgear.skills.comply.parser import ComplianceSpec, Detector, Step

        step_a = Step(
            id="step_a",
            description="first step",
            required=True,
            detector=Detector(description="detect a"),
        )
        step_b = Step(
            id="step_b",
            description="second step",
            required=True,
            detector=Detector(description="detect b", after_step="step_a"),
        )
        return ComplianceSpec(
            id="order-test",
            name="Order Test",
            source_rule="rule",
            version="1.0",
            steps=(step_a, step_b),
            threshold_promote_to_hook=0.6,
        )

    def _make_event(self, ts: str, tool: str = "Bash") -> ObservationEvent:
        """指定タイムスタンプの観測イベントを返す。"""
        from devgear.skills.comply.parser import ObservationEvent

        return ObservationEvent(
            timestamp=ts,
            event="tool_complete",
            tool=tool,
            session="sess",
            input="",
            output="",
        )

    @patch("devgear.skills.comply.grader.classify_events")
    def test_after_step_violation_same_timestamp(self, mock_cls) -> None:
        """event.timestamp == latest_after の場合、after_step 制約違反となり step_b は未検出になる。

        行 40-44 の ``event.timestamp <= latest_after`` が True になるパスを踏む。
        """
        spec = self._make_spec_with_after()
        event_a = self._make_event("2026-01-01T00:00:10Z")
        event_b = self._make_event("2026-01-01T00:00:10Z")  # same timestamp as a → violation
        trace = [event_a, event_b]

        # step_a → index 0, step_b → index 1 として分類
        mock_cls.return_value = {"step_a": [0], "step_b": [1]}

        result = grade(spec, trace)

        step_b_result = next(s for s in result.steps if s.step_id == "step_b")
        assert step_b_result.detected is False
        assert step_b_result.failure_reason is not None
        assert "step_a" in step_b_result.failure_reason

    @patch("devgear.skills.comply.grader.classify_events")
    def test_after_step_violation_earlier_timestamp(self, mock_cls) -> None:
        """event.timestamp < latest_after の場合も after_step 制約違反となる。"""
        spec = self._make_spec_with_after()
        event_a = self._make_event("2026-01-01T00:00:20Z")
        event_b = self._make_event("2026-01-01T00:00:10Z")  # before a → violation
        trace = [event_a, event_b]

        # grade() は trace を時系列順にソートするため、step_a は後続イベントに割り当てる。
        mock_cls.return_value = {"step_a": [1], "step_b": [0]}

        result = grade(spec, trace)

        step_b_result = next(s for s in result.steps if s.step_id == "step_b")
        assert step_b_result.detected is False

    @patch("devgear.skills.comply.grader.classify_events")
    def test_after_step_satisfied_later_timestamp(self, mock_cls) -> None:
        """event.timestamp > latest_after の場合は after_step 制約を満たし step_b が検出される。"""
        spec = self._make_spec_with_after()
        event_a = self._make_event("2026-01-01T00:00:10Z")
        event_b = self._make_event("2026-01-01T00:00:20Z")  # after a → OK
        trace = [event_a, event_b]

        mock_cls.return_value = {"step_a": [0], "step_b": [1]}

        result = grade(spec, trace)

        step_b_result = next(s for s in result.steps if s.step_id == "step_b")
        assert step_b_result.detected is True
