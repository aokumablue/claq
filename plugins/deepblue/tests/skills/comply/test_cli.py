"""comply/cli モジュールのテスト。

バグ修正: シナリオ実行・採点時の RuntimeError / TimeoutExpired が
1シナリオの失敗でプロセス全体をクラッシュさせないことを検証する。

デシジョンテーブル:
  run_scenario 失敗:
    - RuntimeError → SKIPPED、他シナリオは継続
    - TimeoutExpired → SKIPPED、他シナリオは継続

  grade 失敗:
    - RuntimeError → SKIPPED、他シナリオは継続

  全シナリオ成功:
    - graded_results に全件追加

  スキルファイル不在:
    - sys.exit(1)
"""

from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

# テスト対象モジュールを先にインポート（reload は使わない）
from devgear.skills.comply import cli
from devgear.skills.comply.grader import ComplianceResult, StepResult
from devgear.skills.comply.parser import ComplianceSpec, Detector, Step
from devgear.skills.comply.runner import ScenarioRun
from devgear.skills.comply.scenario_generator import Scenario


@pytest.fixture()
def skill_file(tmp_path: Path) -> Path:
    """存在するダミースキルファイル。"""
    f = tmp_path / "SKILL.md"
    f.write_text("# Test Skill\n")
    return f


def _make_spec() -> ComplianceSpec:
    step = Step(
        id="s1",
        description="step one",
        required=True,
        detector=Detector(description="det"),
    )
    return ComplianceSpec(
        id="test",
        name="Test",
        source_rule="rule",
        version="1.0",
        steps=(step,),
        threshold_promote_to_hook=0.6,
    )


def _make_scenario(level_name: str = "strict", level: int = 1) -> Scenario:
    return Scenario(
        id=f"scenario-{level_name}",
        level=level,
        level_name=level_name,
        description="desc",
        prompt="do something",
        setup_commands=(),
    )


def _make_run(scenario: Scenario) -> ScenarioRun:
    return ScenarioRun(
        scenario=scenario,
        observations=(),
        sandbox_dir=Path("/tmp/test"),
    )


def _capture_logged_messages(messages: list[str]):
    def _capture(msg, *args, **_kwargs):
        text = str(msg)
        if args:
            text = text % args
        messages.append(text)

    return _capture


def _make_result() -> ComplianceResult:
    sr = StepResult(step_id="s1", detected=True, evidence=(), failure_reason=None)
    return ComplianceResult(
        spec_id="test",
        steps=(sr,),
        compliance_rate=1.0,
        recommend_hook_promotion=False,
        classification={},
    )


class TestCliScenarioErrorHandling:
    """シナリオ実行・採点エラーのハンドリングテスト。"""

    def _patch_context(self, skill_file: Path, run_side, grade_side=None):
        """共通パッチコンテキストを返す。"""
        spec = _make_spec()
        scenarios = [_make_scenario("strict", 1), _make_scenario("standard", 2)]
        grade_fn = grade_side or (lambda *a, **kw: _make_result())

        return (
            spec,
            scenarios,
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", side_effect=run_side),
            patch("devgear.skills.comply.cli.grade", side_effect=grade_fn),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
        )

    def test_run_scenario_runtime_error_skipped(self, skill_file: Path) -> None:
        """run_scenario が RuntimeError を起こした場合、そのシナリオをスキップし続行する。"""
        scenarios = [_make_scenario("strict", 1), _make_scenario("standard", 2)]
        good_run = _make_run(scenarios[1])
        spec = _make_spec()

        call_count = {"n": 0}
        warnings: list[str] = []

        def run_side_effect(scenario, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("claude not found")
            return good_run

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", side_effect=run_side_effect),
            patch("devgear.skills.comply.cli.grade", return_value=_make_result()) as mock_grade,
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.warning", side_effect=_capture_logged_messages(warnings)),
        ):
            cli.main()

        # grade は1回だけ呼ばれる（2件目のシナリオのみ成功）
        assert mock_grade.call_count == 1
        # SKIPPED の警告が出ている
        assert any("SKIPPED" in w for w in warnings)

    def test_run_scenario_timeout_skipped(self, skill_file: Path) -> None:
        """run_scenario が TimeoutExpired を起こした場合もスキップして続行する。"""
        scenarios = [_make_scenario("strict", 1), _make_scenario("standard", 2)]
        good_run = _make_run(scenarios[1])
        spec = _make_spec()

        call_count = {"n": 0}
        warnings: list[str] = []

        def run_side_effect(scenario, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise subprocess.TimeoutExpired(cmd=["claude"], timeout=300)
            return good_run

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", side_effect=run_side_effect),
            patch("devgear.skills.comply.cli.grade", return_value=_make_result()) as mock_grade,
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.warning", side_effect=_capture_logged_messages(warnings)),
        ):
            cli.main()

        # 2件目は成功しているので grade は呼ばれる
        assert mock_grade.call_count == 1
        assert any("SKIPPED" in w for w in warnings)

    def test_grade_runtime_error_skipped(self, skill_file: Path) -> None:
        """grade が RuntimeError を起こした場合もスキップして続行する。"""
        scenarios = [_make_scenario("strict", 1), _make_scenario("standard", 2)]
        good_run = _make_run(scenarios[0])
        spec = _make_spec()

        grade_count = {"n": 0}
        warnings: list[str] = []

        def grade_side_effect(*args, **kwargs):
            grade_count["n"] += 1
            if grade_count["n"] == 1:
                raise RuntimeError("LLM classifier failed")
            return _make_result()

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", return_value=good_run),
            patch("devgear.skills.comply.cli.grade", side_effect=grade_side_effect),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.warning", side_effect=_capture_logged_messages(warnings)),
        ):
            cli.main()

        assert any("SKIPPED" in w for w in warnings)

    def test_all_scenarios_succeed(self, skill_file: Path) -> None:
        """全シナリオ成功時は graded_results に全件追加されること。"""
        scenarios = [_make_scenario("strict", 1), _make_scenario("standard", 2)]
        good_run = _make_run(scenarios[0])
        spec = _make_spec()
        grade_calls: list = []

        def grade_side_effect(*args, **kwargs):
            grade_calls.append(True)
            return _make_result()

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", return_value=good_run),
            patch("devgear.skills.comply.cli.grade", side_effect=grade_side_effect),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
        ):
            cli.main()

        # シナリオ数分 grade が呼ばれること
        assert len(grade_calls) == len(scenarios)

    def test_skill_file_not_found_exits(self, tmp_path: Path) -> None:
        """スキルファイルが存在しない場合 sys.exit(1) を起こすこと。"""
        missing = tmp_path / "missing.md"
        with patch("sys.argv", ["comply", str(missing)]):
            with pytest.raises(SystemExit) as exc:
                cli.main()
        assert exc.value.code == 1


class TestCliDryRun:
    """--dry-run フラグのテスト。

    デシジョンテーブル:
      dry_run=True  → spec/scenarios 生成後に return、実行系関数は呼ばれない
      dry_run=False → 通常フロー（run_scenario・grade が呼ばれる）
    """

    def test_dry_run_skips_execution(self, skill_file: Path) -> None:
        """--dry-run 指定時は run_scenario/grade が呼ばれずに返ること（行 84-90）。"""
        spec = _make_spec()
        scenarios = [_make_scenario("strict", 1)]
        logs: list[str] = []

        with (
            patch("sys.argv", ["comply", str(skill_file), "--dry-run"]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.run_scenario") as mock_run,
            patch("devgear.skills.comply.cli.grade") as mock_grade,
            patch("logging.Logger.info", side_effect=_capture_logged_messages(logs)),
        ):
            cli.main()

        # 実行系関数は呼ばれない
        mock_run.assert_not_called()
        mock_grade.assert_not_called()
        # dry-run メッセージが出力される
        assert any("dry-run" in log for log in logs)

    def test_dry_run_logs_spec_steps(self, skill_file: Path) -> None:
        """--dry-run 時に spec の各ステップがログ出力されること。"""
        spec = _make_spec()
        scenarios = [_make_scenario("strict", 1)]
        logs: list[str] = []

        with (
            patch("sys.argv", ["comply", str(skill_file), "--dry-run"]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.run_scenario"),
            patch("devgear.skills.comply.cli.grade"),
            patch("logging.Logger.info", side_effect=_capture_logged_messages(logs)),
        ):
            cli.main()

        # spec.id と steps の情報がログに含まれる
        joined = "\n".join(logs)
        assert spec.id in joined


class TestCliNoScenariosExecuted:
    """全シナリオがスキップされた場合のテスト。

    デシジョンテーブル:
      graded_results が空 → "No scenarios were executed." 警告を出して return（行 122-124）
      graded_results あり → overall コンプライアンス率を計算する
    """

    def test_all_scenarios_skipped_logs_warning(self, skill_file: Path) -> None:
        """全シナリオが例外でスキップされた場合、警告メッセージを出して終了すること（行 123-124）。"""
        spec = _make_spec()
        scenarios = [_make_scenario("strict", 1)]
        warnings: list[str] = []

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", side_effect=RuntimeError("fail")),
            patch("devgear.skills.comply.cli.grade"),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.warning", side_effect=_capture_logged_messages(warnings)),
        ):
            cli.main()

        assert any("No scenarios were executed" in w for w in warnings)


class TestCliRecommendationMessage:
    """overall コンプライアンス率が threshold を下回った場合の推奨メッセージテスト。

    デシジョンテーブル:
      overall < threshold_promote_to_hook → 推奨メッセージをログ出力（行 128-132）
      overall >= threshold_promote_to_hook → 推奨メッセージは出力しない
    """

    def _make_low_result(self) -> ComplianceResult:
        """コンプライアンス率 0.0 の結果（threshold=0.6 未満）を返す。"""
        sr = StepResult(step_id="s1", detected=False, evidence=(), failure_reason="missing")
        return ComplianceResult(
            spec_id="test",
            steps=(sr,),
            compliance_rate=0.0,
            recommend_hook_promotion=True,
            classification={},
        )

    def test_low_compliance_logs_recommendation(self, skill_file: Path) -> None:
        """overall < threshold の場合、推奨メッセージが出力されること（行 129-132）。"""
        spec = _make_spec()  # threshold_promote_to_hook=0.6
        scenarios = [_make_scenario("strict", 1)]
        good_run = _make_run(scenarios[0])
        infos: list[str] = []

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", return_value=good_run),
            patch("devgear.skills.comply.cli.grade", return_value=self._make_low_result()),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.info", side_effect=_capture_logged_messages(infos)),
        ):
            cli.main()

        assert any("Recommendation" in i for i in infos)

    def test_high_compliance_no_recommendation(self, skill_file: Path) -> None:
        """overall >= threshold の場合、推奨メッセージは出力されないこと。"""
        spec = _make_spec()  # threshold_promote_to_hook=0.6
        scenarios = [_make_scenario("strict", 1)]
        good_run = _make_run(scenarios[0])
        infos: list[str] = []

        with (
            patch("sys.argv", ["comply", str(skill_file)]),
            patch("devgear.skills.comply.cli.generate_spec", return_value=spec),
            patch("devgear.skills.comply.cli.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.cli.generate_report", return_value="# Report"),
            patch("devgear.skills.comply.cli.run_scenario", return_value=good_run),
            patch("devgear.skills.comply.cli.grade", return_value=_make_result()),
            patch.object(Path, "mkdir"),
            patch.object(Path, "write_text"),
            patch("logging.Logger.info", side_effect=_capture_logged_messages(infos)),
        ):
            cli.main()

        assert not any("Recommendation" in i for i in infos)


class TestCliMainBlock:
    """``if __name__ == '__main__':`` ブロックのテスト（行 136）。"""

    def test_main_block_calls_main(self, tmp_path: Path) -> None:
        """__main__ ブロックが main() を呼び出すことを検証する。"""
        spec = _make_spec()
        scenarios = [_make_scenario("strict", 1)]
        skill_path = tmp_path / "SKILL.md"
        skill_path.write_text("# skill\n", encoding="utf-8")

        with (
            patch("sys.argv", ["comply", str(skill_path), "--dry-run"]),
            patch("devgear.skills.comply.spec_generator.generate_spec", return_value=spec),
            patch("devgear.skills.comply.scenario_generator.generate_scenarios", return_value=scenarios),
            patch("devgear.skills.comply.report.generate_report", return_value="# Report"),
        ):
            runpy.run_module("devgear.skills.comply.cli", run_name="__main__")
