"""Markdown形式のコンプライアンスレポートを生成する。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .grader import ComplianceResult
from .parser import ComplianceSpec, ObservationEvent
from .scenario_generator import Scenario


@dataclass(frozen=True)
class _SummaryMeta:
    """_append_summary_section のメタ情報（スキルパス・仕様・結果・閾値・推奨ステップ）。"""

    skill_path: Path
    spec: ComplianceSpec
    results: list
    overall: float
    threshold: float
    promote_steps: list


def generate_report(
    skill_path: Path,
    spec: ComplianceSpec,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
    scenarios: list[Scenario] | None = None,
) -> str:
    """Markdown形式のコンプライアンスレポートを生成する。

    Args:
        skill_path: テスト対象のスキルファイルへのパス。
        spec: 評価に使用したコンプライアンス仕様。
        results: (scenario_level_name, ComplianceResult, observations) のタプル一覧。
        scenarios: プロンプトを含む元のシナリオ定義。
    """
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    overall = _overall_compliance(results)
    threshold = spec.threshold_promote_to_hook
    promote_steps = _steps_to_promote(spec, results, threshold)

    lines: list[str] = []
    lines.append(f"# comply Report: {skill_path.name}")
    lines.append(f"Generated: {now}")
    lines.append("")

    _append_summary_section(lines, _SummaryMeta(
        skill_path=skill_path,
        spec=spec,
        results=results,
        overall=overall,
        threshold=threshold,
        promote_steps=promote_steps,
    ))
    _append_behavioral_sequence_section(lines, spec)
    _append_scenario_results_section(lines, spec, results)

    if scenarios:
        _append_scenario_prompts_section(lines, scenarios)

    if promote_steps:
        _append_hook_promotion_section(lines, spec, results, promote_steps)

    _append_detail_section(lines, spec, results)

    return "\n".join(lines)


def _append_summary_section(lines: list[str], meta: _SummaryMeta) -> None:
    """サマリーセクションを lines に追記する。"""
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|--------|-------|")
    lines.append(f"| Skill | `{meta.skill_path}` |")
    lines.append(f"| Spec | {meta.spec.id} |")
    lines.append(f"| Scenarios | {len(meta.results)} |")
    lines.append(f"| Overall Compliance | {meta.overall:.0%} |")
    lines.append(f"| Threshold | {meta.threshold:.0%} |")

    if meta.promote_steps:
        step_names = ", ".join(meta.promote_steps)
        lines.append(f"| Recommendation | **Promote {step_names} to hooks** |")
    else:
        lines.append("| Recommendation | All steps above threshold — no hook promotion needed |")
    lines.append("")


def _append_behavioral_sequence_section(lines: list[str], spec: ComplianceSpec) -> None:
    """期待される行動シーケンスセクションを lines に追記する。"""
    lines.append("## Expected Behavioral Sequence")
    lines.append("")
    lines.append("| # | Step | Required | Description |")
    lines.append("|---|------|----------|-------------|")
    for i, step in enumerate(spec.steps, 1):
        req = "Yes" if step.required else "No"
        lines.append(f"| {i} | {step.id} | {req} | {step.detector.description} |")
    lines.append("")


def _append_scenario_results_section(
    lines: list[str],
    spec: ComplianceSpec,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
) -> None:
    """シナリオ結果セクションを lines に追記する。"""
    lines.append("## Scenario Results")
    lines.append("")
    lines.append("| Scenario | Compliance | Failed Steps |")
    lines.append("|----------|-----------|----------------|")
    for level_name, result, _obs in results:
        failed = [
            s.step_id
            for s in result.steps
            if not s.detected and any(sp.id == s.step_id and sp.required for sp in spec.steps)
        ]
        failed_str = ", ".join(failed) if failed else "—"
        lines.append(f"| {level_name} | {result.compliance_rate:.0%} | {failed_str} |")
    lines.append("")


def _append_scenario_prompts_section(lines: list[str], scenarios: list[Scenario]) -> None:
    """シナリオプロンプトセクションを lines に追記する。"""
    lines.append("## Scenario Prompts")
    lines.append("")
    for s in scenarios:
        lines.append(f"### {s.level_name} (Level {s.level})")
        lines.append("")
        for prompt_line in s.prompt.splitlines():
            lines.append(f"> {prompt_line}")
        lines.append("")


def _append_hook_promotion_section(
    lines: list[str],
    spec: ComplianceSpec,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
    promote_steps: list[str],
) -> None:
    """フック昇格の推奨事項セクションを lines に追記する。"""
    lines.append("## Advanced: Hook Promotion Recommendations (optional)")
    lines.append("")
    for step_id in promote_steps:
        rate = _step_compliance_rate(step_id, results)
        step = next(s for s in spec.steps if s.id == step_id)
        lines.append(f"- **{step_id}** (compliance {rate:.0%}): {step.description}")
    lines.append("")


def _append_detail_section(
    lines: list[str],
    spec: ComplianceSpec,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
) -> None:
    """シナリオごとの詳細セクション（タイムライン付き）を lines に追記する。"""
    lines.append("## Detail")
    lines.append("")
    for level_name, result, observations in results:
        lines.append(f"### {level_name} (Compliance: {result.compliance_rate:.0%})")
        lines.append("")
        lines.append("| Step | Required | Detected | Reason |")
        lines.append("|------|----------|----------|--------|")
        for sr in result.steps:
            req = "Yes" if any(sp.id == sr.step_id and sp.required for sp in spec.steps) else "No"
            det = "YES" if sr.detected else "NO"
            reason = sr.failure_reason or "—"
            lines.append(f"| {sr.step_id} | {req} | {det} | {reason} |")
        lines.append("")

        if observations:
            _append_timeline_table(lines, result, observations)


def _append_timeline_table(
    lines: list[str],
    result: ComplianceResult,
    observations: list[ObservationEvent],
) -> None:
    """ツール呼び出しタイムラインテーブルを lines に追記する。"""
    index_to_step: dict[int, str] = {}
    for step_id, indices in result.classification.items():
        for idx in indices:
            index_to_step[idx] = step_id

    lines.append(f"**Tool Call Timeline ({len(observations)} calls)**")
    lines.append("")
    lines.append("| # | Tool | Input | Output | Classified As |")
    lines.append("|---|------|-------|--------|------|")
    for i, obs in enumerate(observations):
        step_label = index_to_step.get(i, "—")
        input_summary = obs.input[:100].replace("|", "\\|").replace("\n", " ")
        output_summary = obs.output[:50].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {i} | {obs.tool} | {input_summary} | {output_summary} | {step_label} |")
    lines.append("")


def _overall_compliance(results: list[tuple[str, ComplianceResult, list[ObservationEvent]]]) -> float:
    """全シナリオの平均コンプライアンス率を返す。"""
    if not results:
        return 0.0
    return sum(r.compliance_rate for _, r, _obs in results) / len(results)


def _step_compliance_rate(
    step_id: str,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
) -> float:
    """指定ステップが検出されたシナリオの割合を返す。"""
    detected = sum(1 for _, r, _obs in results for s in r.steps if s.step_id == step_id and s.detected)
    return detected / len(results) if results else 0.0


def _steps_to_promote(
    spec: ComplianceSpec,
    results: list[tuple[str, ComplianceResult, list[ObservationEvent]]],
    threshold: float,
) -> list[str]:
    """検出率がしきい値を下回る必須ステップ（hook 昇格候補）の ID 一覧を返す。"""
    promote = []
    for step in spec.steps:
        if not step.required:
            continue
        rate = _step_compliance_rate(step.id, results)
        if rate < threshold:
            promote.append(step.id)
    return promote
