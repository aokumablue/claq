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

_MANIFEST = Path(__file__).resolve().parents[1] / ".claude-plugin" / "plugin.json"


def test_manifest_leaves_agents_to_auto_discovery_and_declares_skills_commands_as_directories() -> None:
    """manifest に ``agents`` フィールドが無く、``skills`` / ``commands`` は directory 形式で宣言されていること。

    ``agents`` をファイルパス配列に戻すとホスト上で ``Agents (0)`` に退行し、agent へ委譲する
    全 workflow が静かに機能しなくなる。静かに壊れるため人間の目視では気づけない。
    ``skills`` / ``commands`` は directory 形式を schema が受理し、ホスト上でも全件登録される
    （実測: Skills (22) / Commands は skills として集計）。``agents`` と挙動が
    異なるため、同じ形へ揃えようとして壊さないよう契約を固定する。
    ``agents/`` に 9 体の定義が実在することは tests/test_md_references.py の tools 期待マップとの
    完全一致が固定する（auto-discovery はディレクトリの中身だけが根拠になる）。
    """
    manifest = json.loads(_MANIFEST.read_text(encoding="utf-8"))

    assert "agents" not in manifest, (
        "plugin.json に agents を宣言するとホストの loader が各パスを scandir して "
        "ENOTDIR になり Agents (0) へ退行する。agents/ の auto-discovery に任せること"
    )
    assert manifest["skills"] == ["./skills/"]
    assert manifest["commands"] == ["./commands/"]
