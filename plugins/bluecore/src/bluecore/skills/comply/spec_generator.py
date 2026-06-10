"""LLMを用いて、スキルファイルからコンプライアンス仕様を生成する。"""

from __future__ import annotations

import tempfile
from pathlib import Path

import yaml

from ..cli_runner import run_cli
from .parser import ComplianceSpec, parse_spec
from .utils import extract_yaml

PROMPTS_DIR = Path(__file__).parent / "prompts"


def _build_spec_prompt(base_prompt: str, attempt: int, last_error: Exception | None) -> str:
    """試行回数とエラー情報を元に spec_generator 用プロンプトを返す。"""
    prompt = base_prompt
    if attempt > 0 and last_error is not None:
        prompt += (
            f"\n\nPREVIOUS ATTEMPT FAILED with YAML parse error:\n"
            f"{last_error}\n\n"
            f"Please fix the YAML. Remember to quote all string values "
            f'that contain colons, e.g.: description: "Use type: description format"'
        )
    return prompt


def _call_spec_cli(prompt: str, model: str) -> str:
    """LLM CLI を呼び出し、テキスト出力を返す。エラー時は RuntimeError を送出する。"""
    result = run_cli(
        ["-p", prompt, "--model", model, "--output-format", "text"],
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError(f"llm-cli failed: {result.stderr}")
    return result.stdout


def _parse_spec_with_tempfile(raw_yaml: str) -> ComplianceSpec:
    """YAML テキストを一時ファイル経由で解析し、ComplianceSpec を返す。"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(raw_yaml)
        tmp_path = Path(f.name)
    try:
        return parse_spec(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def generate_spec(
    skill_path: Path,
    model: str = "haiku",
    max_retries: int = 2,
) -> ComplianceSpec:
    """スキル／ルールファイルからコンプライアンス仕様を生成する。

    spec_generator プロンプトで LLM CLI を呼び出し、YAML出力を解析する。
    YAML解析エラー時は、エラーフィードバックを付けて再試行する。

    max_retries に負数を渡すと ValueError を送出する。
    """
    if max_retries < 0:
        raise ValueError(f"max_retries must be >= 0, got {max_retries}")

    skill_content = skill_path.read_text(encoding="utf-8")
    prompt_template = (PROMPTS_DIR / "spec_generator.md").read_text(encoding="utf-8")
    base_prompt = prompt_template.replace("{skill_content}", skill_content)

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        prompt = _build_spec_prompt(base_prompt, attempt, last_error)
        stdout = _call_spec_cli(prompt, model)
        raw_yaml = extract_yaml(stdout)

        try:
            return _parse_spec_with_tempfile(raw_yaml)
        except (yaml.YAMLError, KeyError, TypeError) as e:
            last_error = e
            if attempt == max_retries:
                raise

    # range(max_retries + 1) は max_retries >= 0 のとき必ず1回以上実行されるため
    # ここには到達しない（上記バリデーションで保証）
    raise AssertionError("unreachable")  # pragma: no cover
