"""LLM CLI でシナリオを実行し、ツール呼び出しを解析する。

claude 環境: stream-json 出力をリアルタイム解析。
copilot 環境: json 出力を完了後に一括解析。
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..cli_runner import build_output_format_args, build_tools_args, detect_cli_binary
from .parser import ObservationEvent
from .scenario_generator import Scenario

SANDBOX_BASE = Path(tempfile.gettempdir()) / "comply-sandbox"
ALLOWED_MODELS = frozenset({"haiku", "sonnet", "opus"})
_ALLOWED_TOOLS = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]


@dataclass(frozen=True)
class ScenarioRun:
    """シナリオ1回の実行結果（観測イベントとサンドボックス）を表す。"""

    scenario: Scenario
    observations: tuple[ObservationEvent, ...]
    sandbox_dir: Path


def _build_run_cmd(binary: str, scenario: Scenario, model: str, max_turns: int, sandbox_dir: Path) -> list[str]:
    """LLM CLI 実行コマンドのリストを組み立てて返す。"""
    cmd = [
        binary,
        "-p",
        scenario.prompt,
        "--model",
        model,
        "--max-turns",
        str(max_turns),
        "--add-dir",
        str(sandbox_dir),
        *build_tools_args(binary, _ALLOWED_TOOLS),
        *build_output_format_args(binary, "stream-json"),
    ]
    if binary == "claude":
        cmd.append("--verbose")
    return cmd


def run_scenario(
    scenario: Scenario,
    model: str = "sonnet",
    max_turns: int = 30,
    timeout: int = 300,
) -> ScenarioRun:
    """シナリオを実行し、ツール呼び出しを抽出する。

    claude 環境では stream-json、copilot 環境では json を使用する。
    """
    if model not in ALLOWED_MODELS:
        raise ValueError(f"Unknown model: {model!r}. Allowed: {ALLOWED_MODELS}")

    binary = detect_cli_binary()
    sandbox_dir = _safe_sandbox_dir(scenario.id)
    _setup_sandbox(sandbox_dir, scenario)

    cmd = _build_run_cmd(binary, scenario, model, max_turns, sandbox_dir)
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=sandbox_dir,
    )

    if result.returncode != 0:
        raise RuntimeError(f"llm-cli failed (rc={result.returncode}): {result.stderr[:500]}")

    # copilot --output-format json も claude stream-json と同じ形式のため共通処理
    observations = _parse_stream_json(result.stdout)

    return ScenarioRun(
        scenario=scenario,
        observations=tuple(observations),
        sandbox_dir=sandbox_dir,
    )


def _safe_sandbox_dir(scenario_id: str) -> Path:
    """シナリオIDをサニタイズし、パスがサンドボックス基点内に収まることを保証する。"""
    safe_id = re.sub(r"[^a-zA-Z0-9\-_]", "_", scenario_id)
    path = SANDBOX_BASE / safe_id
    # パスがサンドボックス基点内にあることを検証（パストラバーサル時はValueError）
    path.resolve().relative_to(SANDBOX_BASE.resolve())
    return path


def _setup_sandbox(sandbox_dir: Path, scenario: Scenario) -> None:
    """サンドボックスディレクトリを作成し、セットアップコマンドを実行する。"""
    if sandbox_dir.exists():
        shutil.rmtree(sandbox_dir)
    sandbox_dir.mkdir(parents=True)

    subprocess.run(["git", "init"], cwd=sandbox_dir, capture_output=True)

    for cmd in scenario.setup_commands:
        parts = shlex.split(cmd)
        subprocess.run(parts, cwd=sandbox_dir, capture_output=True)


def _process_assistant_message(msg: dict, pending: dict[str, dict], event_counter: int) -> int:
    """assistant メッセージから tool_use ブロックを pending に登録し、更新後のカウンタを返す。"""
    content = msg.get("message", {}).get("content", [])
    for block in content:
        if block.get("type") == "tool_use":
            tool_use_id = block.get("id", "")
            tool_input = block.get("input", {})
            input_str = (
                json.dumps(tool_input)[:5000] if isinstance(tool_input, dict) else str(tool_input)[:5000]
            )
            pending[tool_use_id] = {
                "tool": block.get("name", "unknown"),
                "input": input_str,
                "order": event_counter,
            }
            event_counter += 1
    return event_counter


def _process_user_message(msg: dict, pending: dict[str, dict], events: list[ObservationEvent]) -> None:
    """user メッセージから tool_result ブロックを取り出し、ObservationEvent を events に追加する。"""
    content = msg.get("message", {}).get("content", [])
    if not isinstance(content, list):
        return
    for block in content:
        tool_use_id = block.get("tool_use_id", "")
        if tool_use_id not in pending:
            continue
        info = pending.pop(tool_use_id)
        output_content = block.get("content", "")
        if isinstance(output_content, list):
            output_str = json.dumps(output_content)[:5000]
        else:
            output_str = str(output_content)[:5000]
        events.append(
            ObservationEvent(
                timestamp=f"T{info['order']:04d}",
                event="tool_complete",
                tool=info["tool"],
                session=msg.get("session_id", "unknown"),
                input=info["input"],
                output=output_str,
            )
        )


def _parse_stream_json(stdout: str) -> list[ObservationEvent]:
    """claude の stream-json 出力を ObservationEvent に変換する。

    stream-json の形式:
    - type=assistant かつ content[].type=tool_use → ツール呼び出し（name, input）
    - type=user かつ content[].type=tool_result → ツール実行結果（output）
    """
    events: list[ObservationEvent] = []
    pending: dict[str, dict] = {}
    event_counter = 0

    for line in stdout.strip().splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        msg_type = msg.get("type")
        if msg_type == "assistant":
            event_counter = _process_assistant_message(msg, pending, event_counter)
        elif msg_type == "user":
            _process_user_message(msg, pending, events)

    for _tool_use_id, info in pending.items():
        events.append(
            ObservationEvent(
                timestamp=f"T{info['order']:04d}",
                event="tool_complete",
                tool=info["tool"],
                session="unknown",
                input=info["input"],
                output="",
            )
        )

    return sorted(events, key=lambda e: e.timestamp)


