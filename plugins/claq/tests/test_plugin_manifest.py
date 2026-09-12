"""`.claude-plugin/plugin.json` がホストへ全 component を登録できる形であることを検証する。

Claude Code 2.1.220〜2.1.247 で観測した非互換への回帰テスト。manifest の
``agents`` にファイルパス配列を書くと ``claude plugin validate --strict`` は
成功する一方、ランタイムの loader は各エントリを directory として ``scandir``
するため全 agent が ``ENOTDIR`` で読み込まれず ``Agents (0)`` になる。
``skills`` / ``commands`` と同じ directory 形式（``["./agents/"]``）は manifest
schema 側が ``agents: Invalid input`` で拒否するため採れない。したがって
``agents`` フィールドを置かず、既定の ``agents/`` auto-discovery に任せるのが
唯一ホストへ 9 体すべてを登録できる形である。

このテストは `claude` CLI に依存しない。CLI 依存のテストは CLI が無い環境で
skip され、リリースゲートとして機能しないため。
"""

from __future__ import annotations

import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MANIFEST = _ROOT / ".claude-plugin" / "plugin.json"


def _load_manifest() -> dict[str, object]:
    """plugin.json を読み込んで返す。"""
    return json.loads(_MANIFEST.read_text(encoding="utf-8"))


def test_manifest_does_not_declare_agents() -> None:
    """manifest に ``agents`` フィールドが無いこと（auto-discovery へ委ねる）。

    ファイルパス配列に戻すとホスト上で ``Agents (0)`` に退行し、agent へ委譲する
    全 workflow が静かに機能しなくなる。静かに壊れるため人間の目視では気づけない。
    """
    manifest = _load_manifest()
    assert "agents" not in manifest, (
        "plugin.json に agents を宣言するとホストの loader が各パスを scandir して "
        "ENOTDIR になり Agents (0) へ退行する。agents/ の auto-discovery に任せること"
    )


def test_agent_surface_exists_on_disk() -> None:
    """manifest から外した後も ``agents/`` に 9 体の定義が実在すること。

    auto-discovery はディレクトリの中身だけが根拠になるので、manifest による
    列挙という安全網が無い。ここで surface の欠落を検出する。
    """
    agent_files = sorted(p.name for p in (_ROOT / "agents").glob("*.md"))
    assert agent_files == [
        "bench-analyzer.md",
        "code-refiner.md",
        "comparator.md",
        "grader.md",
        "harness-tuner.md",
        "planner.md",
        "reviewer.md",
        "security-auditor.md",
        "tdd-writer.md",
    ]


def test_skills_and_commands_use_directory_form() -> None:
    """``skills`` / ``commands`` は directory 形式で宣言されていること。

    この 2 つは directory 形式を schema が受理し、ホスト上でも全件登録される
    （実測: Skills (22) / Commands は skills として集計）。``agents`` と挙動が
    異なるため、同じ形へ揃えようとして壊さないよう契約を固定する。
    """
    manifest = _load_manifest()
    assert manifest["skills"] == ["./skills/"]
    assert manifest["commands"] == ["./commands/"]
