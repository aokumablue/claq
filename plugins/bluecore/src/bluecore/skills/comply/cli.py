"""comply のCLIエントリーポイント。"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from .grader import grade
from .report import generate_report
from .runner import run_scenario
from .scenario_generator import generate_scenarios
from .spec_generator import generate_spec

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    """comply CLI 用の ArgumentParser を構築して返す。"""
    parser = argparse.ArgumentParser(
        description="comply: Measure skill compliance rates",
    )
    parser.add_argument("skill", type=Path, help="Path to skill/rule file to test")
    parser.add_argument("--model", default="sonnet", help="Model for scenario execution (default: sonnet)")
    parser.add_argument("--gen-model", default="haiku", help="Model for spec/scenario generation (default: haiku)")
    parser.add_argument("--dry-run", action="store_true", help="Generate spec and scenarios without executing")
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Output report path (default: results/<skill-name>.md)",
    )
    return parser


def _generate_spec_and_scenarios(args: argparse.Namespace) -> tuple:
    """仕様とシナリオを生成し、ログ出力付きでタプルとして返す。"""
    logger.info("[1/4] Generating compliance spec from %s...", args.skill.name)
    spec = generate_spec(args.skill, model=args.gen_model)
    logger.info("       %d steps extracted", len(spec.steps))

    spec_yaml = yaml.dump(
        {"steps": [{"id": s.id, "description": s.description, "required": s.required} for s in spec.steps]}
    )
    logger.info("[2/4] Generating scenarios (3 prompt strictness levels)...")
    scenarios = generate_scenarios(args.skill, spec_yaml, model=args.gen_model)
    logger.info("       %d scenarios generated", len(scenarios))
    for s in scenarios:
        logger.info("       - %s: %s", s.level_name, s.description[:60])
    return spec, scenarios


def _dry_run_report(spec: Any, scenarios: list) -> None:
    """dry-run 時に仕様とシナリオをログ出力する。"""
    logger.info("\n[dry-run] Spec and scenarios generated. Skipping execution.")
    logger.info("\nSpec: %s (%d steps)", spec.id, len(spec.steps))
    for step in spec.steps:
        marker = "*" if step.required else " "
        logger.info("  [%s] %s: %s", marker, step.id, step.description)


def _execute_scenarios(args: argparse.Namespace, spec: Any, scenarios: list) -> list[tuple[str, Any, list[Any]]]:
    """各シナリオを実行して採点結果のリストを返す。"""
    logger.info("[3/4] Executing scenarios (model=%s)...", args.model)
    graded_results: list[tuple[str, Any, list[Any]]] = []
    for scenario in scenarios:
        logger.info("       Running %s...", scenario.level_name)
        try:
            run = run_scenario(scenario, model=args.model)
        except (RuntimeError, subprocess.TimeoutExpired) as e:
            logger.warning("       %s: SKIPPED (runner error: %s)", scenario.level_name, e)
            continue
        try:
            result = grade(spec, list(run.observations))
        except RuntimeError as e:
            logger.warning("       %s: SKIPPED (grader error: %s)", scenario.level_name, e)
            continue
        graded_results.append((scenario.level_name, result, list(run.observations)))
        logger.info("       %s: %.0f%%", scenario.level_name, result.compliance_rate * 100)
    return graded_results


def _save_report(args: argparse.Namespace, spec: Any, graded_results: list, scenarios: list, results_dir: Path) -> None:
    """レポートを生成してファイルに保存し、最終サマリーをログ出力する。"""
    skill_name = args.skill.parent.name if args.skill.stem == "SKILL" else args.skill.stem
    output_path = args.output or results_dir / f"{skill_name}.md"
    logger.info("[4/4] Generating report...")
    report = generate_report(args.skill, spec, graded_results, scenarios=scenarios)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    logger.info("       Report saved to %s", output_path)

    if not graded_results:
        logger.warning("No scenarios were executed.")
        return
    overall = sum(r.compliance_rate for _, r, _obs in graded_results) / len(graded_results)
    logger.info("\n%s", "=" * 50)
    logger.info("Overall Compliance: %.0f%%", overall * 100)
    if overall < spec.threshold_promote_to_hook:
        logger.info(
            "Recommendation: Some steps have low compliance. "
            "Consider promoting them to hooks. See the report for details."
        )


def main() -> None:
    """comply CLI のエントリポイント。引数を解析してスキルのコンプライアンス計測を実行する。"""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    parser = _build_parser()
    args = parser.parse_args()

    if not args.skill.is_file():
        logger.error("Error: Skill file not found: %s", args.skill)
        sys.exit(1)

    results_dir = Path(__file__).parent / "results"
    results_dir.mkdir(exist_ok=True)

    spec, scenarios = _generate_spec_and_scenarios(args)

    if args.dry_run:
        _dry_run_report(spec, scenarios)
        return

    graded_results = _execute_scenarios(args, spec, scenarios)
    _save_report(args, spec, graded_results, scenarios, results_dir)


if __name__ == "__main__":
    main()
