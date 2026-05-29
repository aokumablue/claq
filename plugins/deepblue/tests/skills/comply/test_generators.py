"""comply の生成系モジュールのテスト。"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from devgear.skills.comply import scenario_generator as sg
from devgear.skills.comply import spec_generator as sp
from devgear.skills.comply.parser import ComplianceSpec, Detector, Step

FIXTURES = Path(__file__).resolve().parents[3] / "src" / "devgear" / "skills" / "comply" / "fixtures"


def _make_spec() -> ComplianceSpec:
    detector = Detector(description="detect", before_step="write_impl")
    step = Step(id="write_test", description="Write a test", required=True, detector=detector)
    return ComplianceSpec(
        id="sample",
        name="Sample",
        source_rule="rule",
        version="1.0",
        steps=(step,),
        threshold_promote_to_hook=0.5,
    )


def test_generate_scenarios_sorts_and_renders_prompt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "scenario_generator.md").write_text("skill={skill_content}\nspec={spec_yaml}\n", encoding="utf-8")
    monkeypatch.setattr(sg, "PROMPTS_DIR", prompts_dir)

    captured_prompts: list[str] = []

    def fake_run_cli(args, **kwargs):  # noqa: ANN001
        captured_prompts.append(args[1])
        return SimpleNamespace(
            returncode=0,
            stdout="""
scenarios:
  - id: medium
    level: 2
    level_name: medium
    description: Middle
    prompt: "  middle  "
    setup_commands: ["echo middle"]
  - id: strict
    level: 1
    level_name: strict
    description: Strict
    prompt: "strict"
    setup_commands: []
""",
            stderr="",
        )

    monkeypatch.setattr(sg, "run_cli", fake_run_cli)
    monkeypatch.setattr(sg, "extract_yaml", lambda text: text)

    scenarios = sg.generate_scenarios(skill_path, "steps: []", model="haiku")

    assert [scenario.level for scenario in scenarios] == [1, 2]
    assert scenarios[0].prompt == "strict"
    assert scenarios[1].setup_commands == ("echo middle",)
    assert "skill=# skill" in captured_prompts[0]
    assert "spec=steps: []" in captured_prompts[0]


@pytest.mark.parametrize(
    ("returncode", "stdout", "match"),
    [
        (1, "oops", "llm-cli failed"),
        (0, "   ", "empty output"),
    ],
)
def test_generate_scenarios_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: str, match: str) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "scenario_generator.md").write_text("{skill_content}\n{spec_yaml}\n", encoding="utf-8")
    monkeypatch.setattr(sg, "PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(sg, "extract_yaml", lambda text: text)
    monkeypatch.setattr(
        sg,
        "run_cli",
        lambda *args, **kwargs: SimpleNamespace(returncode=returncode, stdout=stdout, stderr="boom"),
    )

    with pytest.raises(RuntimeError, match=match):
        sg.generate_scenarios(skill_path, "steps: []")


def test_generate_spec_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "spec_generator.md").write_text("skill={skill_content}\n", encoding="utf-8")
    monkeypatch.setattr(sp, "PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(sp, "extract_yaml", lambda text: text)

    prompts: list[str] = []
    parsed_spec = sp.parse_spec(FIXTURES / "tdd_spec.yaml")
    attempts = {"count": 0}

    def fake_run_cli(args, **kwargs):  # noqa: ANN001
        prompts.append(args[1])
        return SimpleNamespace(
            returncode=0,
            stdout="""
id: sample
name: Sample
source_rule: rule
version: "1.0"
scoring:
  threshold_promote_to_hook: 0.5
steps: []
""",
            stderr="",
        )

    def fake_parse(path):  # noqa: ANN001
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise yaml.YAMLError("bad yaml")
        return parsed_spec

    monkeypatch.setattr(sp, "run_cli", fake_run_cli)
    monkeypatch.setattr(sp, "parse_spec", fake_parse)

    result = sp.generate_spec(skill_path, model="haiku", max_retries=1)

    assert result == parsed_spec
    assert len(prompts) == 2
    assert "PREVIOUS ATTEMPT FAILED" in prompts[1]


def test_generate_spec_errors_after_retry_exhaustion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "spec_generator.md").write_text("skill={skill_content}\n", encoding="utf-8")
    monkeypatch.setattr(sp, "PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(sp, "extract_yaml", lambda text: text)
    monkeypatch.setattr(
        sp,
        "run_cli",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="id: sample\n", stderr=""),
    )
    monkeypatch.setattr(sp, "parse_spec", lambda path: (_ for _ in ()).throw(yaml.YAMLError("bad yaml")))

    with pytest.raises(yaml.YAMLError):
        sp.generate_spec(skill_path, max_retries=0)


def test_generate_spec_raises_on_llm_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """llm-cli が失敗した場合の分岐を通す。"""
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "spec_generator.md").write_text("skill={skill_content}\n", encoding="utf-8")
    monkeypatch.setattr(sp, "PROMPTS_DIR", prompts_dir)
    monkeypatch.setattr(
        sp,
        "run_cli",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="boom"),
    )

    with pytest.raises(RuntimeError, match="llm-cli failed"):
        sp.generate_spec(skill_path)


def test_generate_spec_raises_value_error_when_retries_are_negative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """max_retries が負数の場合は ValueError を送出する。"""
    skill_path = tmp_path / "SKILL.md"
    skill_path.write_text("# skill\n", encoding="utf-8")
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    (prompts_dir / "spec_generator.md").write_text("skill={skill_content}\n", encoding="utf-8")
    monkeypatch.setattr(sp, "PROMPTS_DIR", prompts_dir)

    with pytest.raises(ValueError, match="max_retries must be"):
        sp.generate_spec(skill_path, max_retries=-1)
