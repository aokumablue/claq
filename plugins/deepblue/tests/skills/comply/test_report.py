"""report モジュールのテスト。"""

from __future__ import annotations

from pathlib import Path

from devgear.skills.comply.grader import ComplianceResult, StepResult
from devgear.skills.comply.parser import ComplianceSpec, Detector, ObservationEvent, Step
from devgear.skills.comply.report import _overall_compliance, _step_compliance_rate, _steps_to_promote, generate_report
from devgear.skills.comply.scenario_generator import Scenario


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


def _make_scenario(level: int, name: str, prompt: str) -> Scenario:
    return Scenario(
        id=f"scenario-{name}",
        level=level,
        level_name=name,
        description=f"{name} scenario",
        prompt=prompt,
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

    assert "# s-comply Report: skill.md" in report
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
