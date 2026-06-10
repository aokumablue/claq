"""観測トレース（JSONL）とコンプライアンス仕様（YAML）を解析する。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ObservationEvent:
    """観測トレース中の単一イベント（ツール呼び出し）を表す。"""

    timestamp: str
    event: str
    tool: str
    session: str
    input: str
    output: str


@dataclass(frozen=True)
class Detector:
    """ステップ検出条件と時系列制約（before/after）を表す。"""

    description: str
    after_step: str | None = None
    before_step: str | None = None


@dataclass(frozen=True)
class Step:
    """コンプライアンス仕様内の単一ステップを表す。"""

    id: str
    description: str
    required: bool
    detector: Detector


@dataclass(frozen=True)
class ComplianceSpec:
    """採点対象となるコンプライアンス仕様全体を表す。"""

    id: str
    name: str
    source_rule: str
    version: str
    steps: tuple[Step, ...]
    threshold_promote_to_hook: float


def parse_trace(path: Path) -> list[ObservationEvent]:
    """JSONL形式の観測トレースファイルを解析し、時系列順のイベントに変換する。"""
    if not path.is_file():
        raise FileNotFoundError(f"Trace file not found: {path}")

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []

    events: list[ObservationEvent] = []
    for i, line in enumerate(text.splitlines(), 1):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON at line {i}: {e}") from e
        try:
            events.append(
                ObservationEvent(
                    timestamp=raw["timestamp"],
                    event=raw["event"],
                    tool=raw["tool"],
                    session=raw["session"],
                    input=raw.get("input", ""),
                    output=raw.get("output", ""),
                )
            )
        except KeyError as e:
            raise ValueError(f"Missing required field {e} at line {i}") from e

    return sorted(events, key=lambda e: e.timestamp)


def parse_spec(path: Path) -> ComplianceSpec:
    """YAML形式のコンプライアンス仕様ファイルを解析する。"""
    if not path.is_file():
        raise FileNotFoundError(f"Spec file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))

    steps: list[Step] = []
    for s in raw["steps"]:
        d = s["detector"]
        steps.append(
            Step(
                id=s["id"],
                description=s["description"],
                required=s["required"],
                detector=Detector(
                    description=d["description"],
                    after_step=d.get("after_step"),
                    before_step=d.get("before_step"),
                ),
            )
        )

    if "scoring" not in raw:
        raise KeyError("Missing 'scoring' section in compliance spec")

    return ComplianceSpec(
        id=raw["id"],
        name=raw["name"],
        source_rule=raw["source_rule"],
        version=raw["version"],
        steps=tuple(steps),
        threshold_promote_to_hook=raw["scoring"]["threshold_promote_to_hook"],
    )
