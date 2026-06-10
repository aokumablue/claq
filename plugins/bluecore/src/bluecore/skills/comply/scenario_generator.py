"""LLMを用いて、スキルと仕様からプレッシャーシナリオを生成する。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from ..cli_runner import run_cli
from .utils import extract_yaml

PROMPTS_DIR = Path(__file__).parent / "prompts"


@dataclass(frozen=True)
class Scenario:
    """スキルをテストするためのプレッシャーシナリオを表す。"""

    id: str
    level: int
    level_name: str
    description: str
    prompt: str
    setup_commands: tuple[str, ...]


def generate_scenarios(
    skill_path: Path,
    spec_yaml: str,
    model: str = "haiku",
) -> list[Scenario]:
    """プロンプト厳格度を段階的に下げた3つのシナリオを生成する。

    scenario_generator プロンプトで LLM CLI を呼び出し、YAML出力を解析する。
    """
    skill_content = skill_path.read_text(encoding="utf-8")
    prompt_template = (PROMPTS_DIR / "scenario_generator.md").read_text(encoding="utf-8")
    prompt = prompt_template.replace("{skill_content}", skill_content).replace("{spec_yaml}", spec_yaml)

    result = run_cli(
        ["-p", prompt, "--model", model, "--output-format", "text"],
        timeout=120,
    )

    if result.returncode != 0:
        raise RuntimeError(f"llm-cli failed: {result.stderr}")

    if not result.stdout.strip():
        raise RuntimeError("llm-cli returned empty output")

    raw_yaml = extract_yaml(result.stdout)
    parsed = yaml.safe_load(raw_yaml)

    scenarios = [
        Scenario(
            id=s["id"],
            level=s["level"],
            level_name=s["level_name"],
            description=s["description"],
            prompt=s["prompt"].strip(),
            setup_commands=tuple(s.get("setup_commands", [])),
        )
        for s in parsed["scenarios"]
    ]

    return sorted(scenarios, key=lambda s: s.level)
