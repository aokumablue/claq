"""CI 検証モジュールのテスト。

エージェント、コマンド、フック、スキルの検証を対象とする。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ple4.ci.validate_agents as validate_agents
import ple4.ci.validate_commands as validate_commands
import ple4.ci.validate_skills as validate_skills


def test_validate_agents_accepts_valid_agent(tmp_path: Path) -> None:
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "planner.md").write_text(
        "---\nname: planner\ndescription: d\nmodel: sonnet\ntools: Read\n---\n# Planner\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_agents_rejects_agent_without_tools_declaration(tmp_path: Path) -> None:
    """tools frontmatter が無いエージェントは FAIL する（暗黙の全権継承を防ぐため必須）。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent.md").write_text(
        "---\nname: agent\ndescription: A portable custom agent.\n---\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 1


def test_validate_agents_allows_any_custom_tool_names(tmp_path: Path) -> None:
    """tools の値自体は allowlist で縛らない。宣言の有無だけを見る。"""
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    (agents_dir / "agent.md").write_text(
        "---\nname: agent\ndescription: A portable custom agent.\ntools: SomeCustomTool\n---\n",
        encoding="utf-8",
    )

    assert validate_agents.validate_agents(agents_dir) == 0


def test_validate_commands_flags_invalid_references(tmp_path: Path) -> None:
    root = tmp_path
    commands_dir = root / "commands"
    agents_dir = root / "agents"
    skills_dir = root / "skills"
    commands_dir.mkdir()
    agents_dir.mkdir()
    skills_dir.mkdir()
    (commands_dir / "build.md").write_text("Use `/missing-command` and agents/missing.md\n", encoding="utf-8")

    assert validate_commands.validate_commands(root, commands_dir, agents_dir, skills_dir) == 1


def test_validate_skills_accepts_skill_directory(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "planner"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: planner\ndescription: いつ呼ぶかの説明\n---\n\n# Planner\n", encoding="utf-8"
    )

    assert validate_skills.validate_skills(skills_dir) == 0


def _write_skill(skills_dir: Path, frontmatter: str) -> Path:
    """検証用の SKILL.md を 1 つ書き出す。"""
    skill_dir = skills_dir / "planner"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(f"---\n{frontmatter}\n---\n\n# Planner\n", encoding="utf-8")
    return skill_dir


@pytest.mark.parametrize(
    ("label", "value"),
    [("引用符付き", '"true"'), ("yes", "yes"), ("数値", "1")],
)
def test_validate_skills_rejects_non_boolean_flags(tmp_path: Path, label: str, value: str) -> None:
    """真偽値フィールドが bool でなければ不合格になること。

    自前パーサは `"true"` を str、`1` を int として読む。ホストは真の bool しか
    見ないので、これらは**宣言したつもりで効かない**。`disable-model-invocation`
    が効かなければ、スラッシュ起動専用にしたはずの skill が自動発火へ戻る。
    """
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, f"name: planner\ndescription: 説明\ndisable-model-invocation: {value}")

    assert validate_skills.validate_skills(skills_dir) == 1, label


def test_validate_skills_rejects_underscore_misspelling(tmp_path: Path) -> None:
    """ハイフンをアンダースコアに取り違えた綴りが不合格になること。

    自前パーサは綴り違いを「別のキー」として黙って受理するため、宣言は丸ごと
    無視される。効かない宣言は、宣言が無いのと同じかそれより悪い。
    """
    skills_dir = tmp_path / "skills"
    _write_skill(skills_dir, "name: planner\ndescription: 説明\ndisable_model_invocation: true")

    assert validate_skills.validate_skills(skills_dir) == 1


def test_validate_skills_accepts_real_booleans(tmp_path: Path) -> None:
    """引用符なしの true / false は通ること（正しい書き方を落とさない）。"""
    skills_dir = tmp_path / "skills"
    _write_skill(
        skills_dir,
        "name: planner\ndescription: 説明\nuser-invocable: true\ndisable-model-invocation: false",
    )

    assert validate_skills.validate_skills(skills_dir) == 0


def test_quick_skills_stay_slash_only() -> None:
    """quick 系が `disable-model-invocation: true` を宣言し続けること。

    この宣言が落ちると skill は黙って自動発火へ戻る。人間が実行を担うと決めた
    ときだけ入るモードなので、モデルの推測で入ってはならない。型検査は上の
    テストが持つので、ここは**宣言が実在すること**だけを見る。
    """
    from ple4.ci.ci_common import extract_frontmatter

    skills = Path(__file__).resolve().parents[2] / "skills"
    quick = sorted(p for p in skills.glob("quick-*/SKILL.md"))
    assert len(quick) >= 5, f"quick 系が {len(quick)} 件しか見つからない"
    lapsed = [
        p.parent.name
        for p in quick
        if (extract_frontmatter(p.read_text(encoding="utf-8")) or {}).get("disable-model-invocation")
        is not True
    ]
    assert lapsed == [], f"自動発火へ戻っている quick 系: {lapsed}"


def test_extract_frontmatter_skips_lines_without_colon() -> None:
    """コロンが無い frontmatter 行はスキップする。"""
    from ple4.ci.validate_agents import extract_frontmatter

    fm = extract_frontmatter("---\nname: x\nnocolon\n---\nbody")
    assert fm == {"name": "x"}


def test_extract_frontmatter_returns_none_for_non_mapping_frontmatter() -> None:
    """トップレベルが辞書でない frontmatter は None を返す。"""
    from ple4.ci.validate_agents import extract_frontmatter

    assert extract_frontmatter("---\n- item\n---\nbody") is None


def test_validate_skills_requires_frontmatter_fields(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """SKILL.md の frontmatter に name / description が無ければ失敗すること（F-03）。

    agents は各ファイルの frontmatter を検査するのに skills は「存在する・
    読める・空でない」しか見ておらず、非対称が残っていた。
    """
    skills_dir = tmp_path / "skills"
    (skills_dir / "no-frontmatter").mkdir(parents=True)
    (skills_dir / "no-frontmatter" / "SKILL.md").write_text("# heading only\n", encoding="utf-8")

    assert validate_skills.validate_skills(skills_dir) == 1
    assert "フロントマターがありません" in capsys.readouterr().err

    partial = tmp_path / "partial"
    (partial / "half").mkdir(parents=True)
    (partial / "half" / "SKILL.md").write_text("---\nname: half\n---\n\n# Half\n", encoding="utf-8")

    assert validate_skills.validate_skills(partial) == 1
    assert "description がありません" in capsys.readouterr().err
