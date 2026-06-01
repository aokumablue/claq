"""LLM分類を用いて、観測トレースをコンプライアンス仕様に照らして採点する。"""

from __future__ import annotations

from dataclasses import dataclass

from .classifier import classify_events
from .parser import ComplianceSpec, ObservationEvent, Step


@dataclass(frozen=True)
class StepResult:
    """単一ステップの採点結果（検出有無・根拠・失敗理由）を表す。"""

    step_id: str
    detected: bool
    evidence: tuple[ObservationEvent, ...]
    failure_reason: str | None


@dataclass(frozen=True)
class ComplianceResult:
    """1シナリオに対する仕様全体の採点結果を表す。"""

    spec_id: str
    steps: tuple[StepResult, ...]
    compliance_rate: float
    recommend_hook_promotion: bool
    classification: dict[str, list[int]]


def _check_temporal_order(
    step: Step,
    event: ObservationEvent,
    resolved: dict[str, list[ObservationEvent]],
    classified: dict[str, list[ObservationEvent]],
) -> str | None:
    """before_step/after_step 制約を検証する。失敗理由または None を返す。"""
    if step.detector.after_step is not None:
        after_events = resolved.get(step.detector.after_step, [])
        if not after_events:
            return f"after_step '{step.detector.after_step}' not yet detected"
        latest_after = max(e.timestamp for e in after_events)
        if event.timestamp <= latest_after:
            return (
                f"must occur after '{step.detector.after_step}' "
                f"(last at {latest_after}), but found at {event.timestamp}"
            )

    if step.detector.before_step is not None:
        # LLM分類結果を使って先読み判定する
        before_events = resolved.get(step.detector.before_step)
        if before_events is None:
            before_events = classified.get(step.detector.before_step, [])
        if before_events:
            earliest_before = min(e.timestamp for e in before_events)
            if event.timestamp >= earliest_before:
                return (
                    f"must occur before '{step.detector.before_step}' "
                    f"(first at {earliest_before}), but found at {event.timestamp}"
                )

    return None


def _resolve_step_results(
    spec: ComplianceSpec,
    classified: dict[str, list[ObservationEvent]],
) -> list[StepResult]:
    """各ステップの時系列制約を検証し、StepResult のリストを返す。"""
    resolved: dict[str, list[ObservationEvent]] = {}
    step_results: list[StepResult] = []

    for step in spec.steps:
        candidates = classified.get(step.id, [])
        matched: list[ObservationEvent] = []
        failure_reason: str | None = None

        for event in candidates:
            temporal_fail = _check_temporal_order(step, event, resolved, classified)
            if temporal_fail is None:
                matched.append(event)
                break
            else:
                failure_reason = temporal_fail

        detected = len(matched) > 0
        if detected:
            resolved[step.id] = matched
        elif failure_reason is None:
            failure_reason = f"no matching event classified for step '{step.id}'"

        step_results.append(
            StepResult(
                step_id=step.id,
                detected=detected,
                evidence=tuple(matched),
                failure_reason=failure_reason if not detected else None,
            )
        )

    return step_results


def grade(
    spec: ComplianceSpec,
    trace: list[ObservationEvent],
    classifier_model: str = "haiku",
) -> ComplianceResult:
    """LLM分類を用いて、トレースをコンプライアンス仕様に対して採点する。"""
    sorted_trace = sorted(trace, key=lambda e: e.timestamp)

    # 手順1: LLMで全イベントを一括分類する
    classification = classify_events(spec, sorted_trace, model=classifier_model)

    # インデックスをイベントに変換
    classified: dict[str, list[ObservationEvent]] = {
        step_id: [sorted_trace[i] for i in indices if 0 <= i < len(sorted_trace)]
        for step_id, indices in classification.items()
    }

    # 手順2: 時系列順の制約を検証（決定論的）
    step_results = _resolve_step_results(spec, classified)

    required_ids = {s.id for s in spec.steps if s.required}
    required_steps = [s for s in step_results if s.step_id in required_ids]
    detected_required = sum(1 for s in required_steps if s.detected)
    total_required = len(required_steps)

    compliance_rate = detected_required / total_required if total_required > 0 else 0.0

    return ComplianceResult(
        spec_id=spec.id,
        steps=tuple(step_results),
        compliance_rate=compliance_rate,
        recommend_hook_promotion=compliance_rate < spec.threshold_promote_to_hook,
        classification=classification,
    )
