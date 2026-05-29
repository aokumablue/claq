"""agent_compress モジュールの追加テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest

from devgear.lib.agent_compress import (
    build_agent_catalog,
    compress_to_catalog,
    compress_to_summary,
    extract_summary,
    lazy_load_agent,
    load_agent,
    load_agents,
    parse_frontmatter,
)


def _write_agent(
    path: Path,
    *,
    name: str | None = None,
    description: str = "sample agent",
    model: str | None = "sonnet",
    tools: str = '["Read", "Grep"]',
    body: str = "First sentence. Second sentence!",
) -> Path:
    """テスト用の agent ファイルを作成する。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    if name is not None:
        lines.append(f"name: {name}")
    lines.append(f"description: {description}")
    if tools is not None:
        lines.append(f"tools: {tools}")
    if model is not None:
        lines.append(f"model: {model}")
    lines.extend(["---", "", body, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_parse_frontmatter_handles_yaml_arrays_quotes_and_plain_body() -> None:
    """frontmatter の有無と配列/引用符の処理を確認する。"""
    plain = "No frontmatter here"
    assert parse_frontmatter(plain) == {"frontmatter": {}, "body": plain}

    parsed = parse_frontmatter(
        """\
---
name: "Agent One"
tools: ["Read", "Grep"]
bad_tools: [Read, Grep]
---

Body text.
"""
    )

    assert parsed["frontmatter"]["name"] == "Agent One"
    assert parsed["frontmatter"]["tools"] == ["Read", "Grep"]
    assert parsed["frontmatter"]["bad_tools"] == "[Read, Grep]"
    assert parsed["body"] == "\nBody text.\n"


def test_parse_frontmatter_ignores_lines_without_colons_and_extract_summary_flushes_on_headings() -> None:
    """frontmatter の無効行と見出し境界での段落確定を確認する。"""
    parsed = parse_frontmatter(
        """\
---
name: Agent One
nonsense
---

Body text.
"""
    )
    assert parsed["frontmatter"]["name"] == "Agent One"

    assert extract_summary("Paragraph one.\n# Heading\nMore text.") == "Paragraph one."


@pytest.mark.parametrize(
    ("body", "max_sentences", "expected"),
    [
        ("```python\nprint('x')\n```", 1, ""),
        ("# Heading\n- item\n| col |\n", 1, ""),
        ("First sentence. Second sentence! Third?\n\nSecond paragraph.", 2, "First sentence. Second sentence!"),
    ],
)
def test_extract_summary_skips_noise_and_limits_sentences(
    body: str,
    max_sentences: int,
    expected: str,
) -> None:
    """要約抽出が見出し/コード/リストを無視し、文数を制限する。"""
    assert extract_summary(body, max_sentences=max_sentences) == expected


def test_load_agent_defaults_and_load_agents(tmp_path: Path) -> None:
    """load_agent / load_agents の基本分岐を確認する。"""
    agent_dir = tmp_path / "agents"
    _write_agent(agent_dir / "b.md", name=None, tools="not-a-list", model=None, body="Body B.")
    _write_agent(agent_dir / "a.md", name="Alpha", body="Body A.")

    assert load_agents(tmp_path / "missing") == []

    agents = load_agents(agent_dir)
    assert [agent["fileName"] for agent in agents] == ["a", "b"]

    loaded = load_agent(agent_dir / "b.md")
    assert loaded["fileName"] == "b"
    assert loaded["name"] == "b"
    assert loaded["description"] == "sample agent"
    assert loaded["tools"] == []
    assert loaded["model"] == "sonnet"
    assert loaded["body"] == "\nBody B.\n"


def test_compress_helpers_and_catalog_modes(tmp_path: Path) -> None:
    """圧縮出力の各モードと filter_fn を確認する。"""
    agent_dir = tmp_path / "agents"
    _write_agent(agent_dir / "alpha.md", name="Alpha", body="First sentence. Second sentence!")
    _write_agent(agent_dir / "beta.md", name="Beta", body="Only one sentence.")

    agent = load_agent(agent_dir / "alpha.md")
    assert compress_to_catalog(agent) == {
        "name": "Alpha",
        "description": "sample agent",
        "tools": ["Read", "Grep"],
        "model": "sonnet",
    }
    assert compress_to_summary(agent)["summary"] == "First sentence."

    with pytest.raises(ValueError, match="Invalid mode"):
        build_agent_catalog(agent_dir, mode="invalid")

    catalog = build_agent_catalog(agent_dir, mode="catalog")
    assert catalog["stats"]["mode"] == "catalog"
    assert catalog["stats"]["totalAgents"] == 2
    assert "summary" not in catalog["agents"][0]

    summary = build_agent_catalog(agent_dir, mode="summary", filter_fn=lambda item: item["name"] == "Alpha")
    assert summary["stats"]["mode"] == "summary"
    assert summary["stats"]["totalAgents"] == 1
    assert summary["agents"][0]["summary"] == "First sentence."

    full = build_agent_catalog(agent_dir, mode="full", filter_fn=lambda item: item["name"] == "Alpha")
    assert full["stats"]["mode"] == "full"
    assert full["agents"][0]["body"] == "\nFirst sentence. Second sentence!\n"


def test_lazy_load_agent_validates_names_and_path_escape(tmp_path: Path) -> None:
    """lazy_load_agent が不正名、欠損、外部参照を拒否する。"""
    agent_dir = tmp_path / "agents"
    agent_dir.mkdir()
    _write_agent(agent_dir / "alpha.md", name="Alpha")

    outside = tmp_path / "outside.md"
    outside.write_text(
        """\
---
name: Escape
description: outside
tools: ["Read"]
model: sonnet
---

Body.
""",
        encoding="utf-8",
    )
    (agent_dir / "escape.md").symlink_to(outside)

    assert lazy_load_agent(agent_dir, "../alpha") is None
    assert lazy_load_agent(agent_dir, "missing") is None
    assert lazy_load_agent(agent_dir, "escape") is None

    loaded = lazy_load_agent(agent_dir, "alpha")
    assert loaded is not None
    assert loaded["name"] == "Alpha"
