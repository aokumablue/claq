"""report モジュールのテスト。"""

from __future__ import annotations

from pathlib import Path

from bluecore.skills.comply.grader import ComplianceResult, StepResult
from bluecore.skills.comply.parser import ComplianceSpec, Detector, ObservationEvent, Step
from bluecore.skills.comply.report import (
    ScenarioNotMeasured,
    _overall_compliance,
    _step_compliance_rate,
    _steps_to_promote,
    generate_report,
)
from bluecore.skills.comply.scenario_generator import Scenario


def _make_spec(*, threshold: float = 0.75, required: bool = True) -> ComplianceSpec:
    steps = (
        Step(
            id="write_test",
            description="write tests",
            required=required,
            detector=Detector(description="detect test writing"),
        ),
        Step(
            id="refactor",
            description="refactor code",
            required=False,
            detector=Detector(description="detect refactoring"),
        ),
    )
    return ComplianceSpec(
        id="comply-spec",
        name="Compy Spec",
        source_rule="rule",
        version="1.0",
        steps=steps,
        threshold_promote_to_hook=threshold,
    )


def _make_event(timestamp: str, tool: str, input_text: str, output_text: str) -> ObservationEvent:
    return ObservationEvent(
        timestamp=timestamp,
        event="tool_complete",
        tool=tool,
        session="session-1",
        input=input_text,
        output=output_text,
    )


def _make_scenario(level: int, name: str, prompt: str, required_tools: tuple[str, ...] = ("Read",)) -> Scenario:
    return Scenario(
        id=f"scenario-{name}",
        level=level,
        level_name=name,
        description=f"{name} scenario",
        prompt=prompt,
        required_tools=required_tools,
        setup_commands=(),
    )


def test_generate_report_includes_promotions_and_timeline(tmp_path: Path) -> None:
    spec = _make_spec()
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("# Skill", encoding="utf-8")

    strict_event_1 = _make_event("2026-01-01T00:00:01Z", "Read", "open | file\nagain", "ok")
    strict_event_2 = _make_event("2026-01-01T00:00:02Z", "Write", "add tests", "done")
    relaxed_event = _make_event("2026-01-01T00:00:03Z", "Run", "pytest", "failed")

    results = [
        (
            "strict",
            ComplianceResult(
                spec_id=spec.id,
                steps=(
                    StepResult(step_id="write_test", detected=True, evidence=(strict_event_1,), failure_reason=None),
                    StepResult(step_id="refactor", detected=True, evidence=(strict_event_2,), failure_reason=None),
                ),
                compliance_rate=1.0,
                recommend_hook_promotion=False,
                classification={"write_test": [0], "refactor": [1]},
            ),
            [strict_event_1, strict_event_2],
        ),
        (
            "relaxed",
            ComplianceResult(
                spec_id=spec.id,
                steps=(
                    StepResult(step_id="write_test", detected=False, evidence=(), failure_reason="missing"),
                    StepResult(step_id="refactor", detected=False, evidence=(), failure_reason="optional missing"),
                ),
                compliance_rate=0.0,
                recommend_hook_promotion=True,
                classification={},
            ),
            [relaxed_event],
        ),
    ]

    report = generate_report(
        skill_path,
        spec,
        results,
        [
            _make_scenario(1, "strict", "first line\nsecond line"),
            _make_scenario(2, "relaxed", "only one line"),
        ],
    )

    assert "# comply Report: skill.md" in report
    assert "Generated:" in report
    assert "| Overall Compliance | 50% |" in report
    assert "| Recommendation | **Promote write_test to hooks** |" in report
    assert "## Scenario Prompts" in report
    assert "> first line" in report
    assert "> second line" in report
    assert "### strict (Compliance: 100%)" in report
    assert "| strict | 100% | — |" in report
    assert "| relaxed | 0% | write_test |" in report
    assert "**Tool Call Timeline (2 calls)**" in report
    assert "| 0 | Read | open \\| file again | ok | write_test |" in report
    assert "| 1 | Write | add tests | done | refactor |" in report
    assert "| 0 | Run | pytest | failed | — |" in report


def test_generate_report_without_promotions_or_scenarios() -> None:
    spec = _make_spec(required=False, threshold=0.5)

    report = generate_report(Path("/tmp/skill.md"), spec, [])

    assert "| Recommendation | All steps above threshold — no hook promotion needed |" in report
    assert "## Scenario Prompts" not in report
    assert "## Advanced: Hook Promotion Recommendations (optional)" not in report
    assert _overall_compliance([]) == 0.0
    assert _step_compliance_rate("write_test", []) == 0.0
    assert _steps_to_promote(spec, [], 0.5) == []


def test_generate_report_no_promotion_and_no_observations(tmp_path: Path) -> None:
    """全ステップ検出（promote無し）かつ observations 空のレポート。"""
    spec = _make_spec()
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("# Skill", encoding="utf-8")
    results = [
        (
            "strict",
            ComplianceResult(
                spec_id=spec.id,
                steps=(
                    StepResult(step_id="write_test", detected=True, evidence=(), failure_reason=None),
                    StepResult(step_id="refactor", detected=True, evidence=(), failure_reason=None),
                ),
                compliance_rate=1.0,
                recommend_hook_promotion=False,
                classification={},
            ),
            [],
        ),
    ]
    report = generate_report(skill_path, spec, results)
    assert "comply Report" in report


def test_generate_report_without_not_measured_omits_section() -> None:
    """not_measured 未指定（None）の場合、'Not Measured' セクションは出力されず、
    サマリーの未計測件数は 0 と表示されること。
    """
    spec = _make_spec(required=False, threshold=0.5)

    report = generate_report(Path("/tmp/skill.md"), spec, [])

    assert "## Not Measured (Unsupported Tools)" not in report
    assert "| Not Measured (unsupported tools) | 0 |" in report


def test_generate_report_with_not_measured_scenarios(tmp_path: Path) -> None:
    """not_measured が指定された場合、専用セクションとサマリー件数に反映され、
    PASS/FAIL の Scenario Results テーブルには影響しないこと。

    修正前は UnsupportedScenarioError / ScenarioNotMeasured という概念が存在せず、
    ツール不足のシナリオは「未計測」として区別する手段がなかった。
    """
    spec = _make_spec()
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("# Skill", encoding="utf-8")

    strict_event = _make_event("2026-01-01T00:00:01Z", "Read", "read file", "ok")
    results = [
        (
            "strict",
            ComplianceResult(
                spec_id=spec.id,
                steps=(
                    StepResult(step_id="write_test", detected=True, evidence=(strict_event,), failure_reason=None),
                    StepResult(step_id="refactor", detected=True, evidence=(), failure_reason=None),
                ),
                compliance_rate=1.0,
                recommend_hook_promotion=False,
                classification={"write_test": [0]},
            ),
            [strict_event],
        ),
    ]
    not_measured = [
        ScenarioNotMeasured(
            scenario=_make_scenario(2, "relaxed", "only one line", required_tools=("Read", "WebFetch")),
            unsupported_tools=("WebFetch",),
            reason="Required tools are unavailable in this environment",
        ),
    ]

    report = generate_report(skill_path, spec, results, not_measured=not_measured)

    assert "## Not Measured (Unsupported Tools)" in report
    assert "| relaxed | WebFetch | Required tools are unavailable in this environment |" in report
    assert "| Not Measured (unsupported tools) | 1 |" in report
    # PASS/FAIL 集計は measured の1件のみで、100% のまま（未計測分に引きずられない）
    assert "| Overall Compliance | 100% |" in report
    assert "| Scenarios | 1 |" in report
