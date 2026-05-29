"""devgear.ci.catalog_check のテスト。"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from devgear.ci import catalog_check


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_build_catalog_and_list_matching_files(tmp_path: Path) -> None:
    (tmp_path / "agents").mkdir()
    write_text(tmp_path / "agents" / "alpha.md", "# alpha\n")
    write_text(tmp_path / "agents" / "ignored.txt", "skip\n")
    (tmp_path / "commands").mkdir()
    write_text(tmp_path / "commands" / "build.md", "# build\n")
    (tmp_path / "skills" / "planner").mkdir(parents=True)
    write_text(tmp_path / "skills" / "planner" / "SKILL.md", "# planner\n")

    assert catalog_check.list_matching_files(
        "agents", lambda entry: entry.is_file(follow_symlinks=False) and entry.name.endswith(".md"), tmp_path
    ) == ["agents/alpha.md"]

    catalog = catalog_check.build_catalog(tmp_path)
    assert catalog["agents"]["count"] == 1
    assert catalog["commands"]["files"] == ["commands/build.md"]
    assert catalog["skills"]["files"] == ["skills/planner/SKILL.md"]
    assert catalog_check.list_matching_files("missing", lambda entry: True, tmp_path) == []


def test_read_file_or_throw_and_normalize_path(tmp_path: Path) -> None:
    file_path = tmp_path / "nested" / "file.md"
    write_text(file_path, "content\n")

    assert catalog_check._normalize_path_segments(r"agents\alpha.md") == "agents/alpha.md"
    assert catalog_check.read_file_or_throw(file_path) == "content\n"

    with pytest.raises(RuntimeError, match="missing.md の読み取りに失敗しました"):
        catalog_check.read_file_or_throw(tmp_path / "missing.md")


def test_parse_expectations_and_errors() -> None:
    readme = """
    access to 3 agents, 4 skills, and 5 commands

    | **Agents** | PASS: 3 agents | ok |
    | Commands | 5 commands | ok |
    | Skills | ✅ 4 skills | ok |
    """
    claude = """
    - **agents/** - 3 個の専門サブエージェント
    - **skills/** - 4 個のワークフロー定義とドメイン知識
    - **commands/** - 5 個のスラッシュコマンド
    """

    readme_expectations = catalog_check.parse_readme_expectations(readme)
    claude_expectations = catalog_check.parse_claude_doc_expectations(claude)

    assert [item["expected"] for item in readme_expectations] == [3, 4, 5, 3, 5, 4]
    assert [item["category"] for item in claude_expectations] == ["agents", "skills", "commands"]

    with pytest.raises(RuntimeError, match="クイックスタートのカタログ要約"):
        catalog_check.parse_readme_expectations("no summary")

    with pytest.raises(RuntimeError, match="README.md の比較表 に エージェント 行がありません"):
        catalog_check.parse_readme_expectations("access to 1 agents, 1 skills, and 1 commands")

    with pytest.raises(RuntimeError, match="CLAUDE.md のプロジェクト構成 に スキル エントリがありません"):
        catalog_check.parse_claude_doc_expectations("- **agents/** - 1 個の専門サブエージェント")


def test_evaluate_and_render_expectations(capsys: pytest.CaptureFixture[str]) -> None:
    catalog = {
        "agents": {"count": 2, "glob": "agents/*.md"},
        "commands": {"count": 4, "glob": "commands/*.md"},
        "skills": {"count": 6, "glob": "skills/*/SKILL.md"},
    }
    expectations = [
        {"category": "agents", "mode": "exact", "expected": 2, "source": "README.md quick-start summary"},
        {"category": "commands", "mode": "minimum", "expected": 3, "source": "README.md comparison table"},
        {"category": "skills", "mode": "exact", "expected": 7, "source": "CLAUDE.md project structure"},
    ]

    results = catalog_check.evaluate_expectations(catalog, expectations)
    assert [item["ok"] for item in results] == [True, True, False]
    assert catalog_check.format_expectation(results[-1]) == (
        "CLAUDE.md のプロジェクト構成: スキル の文書化件数は = 7、実際は 6 です"
    )

    catalog_check.render_text({"catalog": catalog, "checks": results})
    stdout = capsys.readouterr()
    assert "カタログ件数:" in stdout.out
    assert "ドキュメント件数の不一致が見つかりました:" in stdout.err

    catalog_check.render_markdown({"catalog": catalog, "checks": results})
    markdown = capsys.readouterr().out
    assert "# devgear カタログ検証" in markdown
    assert "## 不一致" in markdown

    capsys.readouterr()
    catalog_check.render_text(
        {
            "catalog": catalog,
            "checks": [
                {"category": "agents", "mode": "exact", "expected": 2, "source": "README.md quick-start summary", "actual": 2, "ok": True},
            ],
        }
    )
    assert "ドキュメント件数はリポジトリのカタログと一致しています。" in capsys.readouterr().out

    catalog_check.render_markdown(
        {
            "catalog": catalog,
            "checks": [
                {"category": "agents", "mode": "exact", "expected": 2, "source": "README.md quick-start summary", "actual": 2, "ok": True},
            ],
        }
    )
    assert "ドキュメント件数はリポジトリのカタログと一致しています。" in capsys.readouterr().out


def test_main_happy_and_unhappy_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for folder in ("agents", "commands", "skills"):
        (root / folder).mkdir()
    write_text(root / "README.md", "access to 0 agents, 0 skills, and 0 commands\n| Agents | 0 agents |\n| Commands | 0 commands |\n| Skills | 0 skills |\n")
    write_text(
        root / "CLAUDE.md",
        "- **agents/** - 0 個の専門サブエージェント\n- **skills/** - 0 個のワークフロー定義とドメイン知識\n- **commands/** - 0 個のスラッシュコマンド\n",
    )

    assert catalog_check.main(["--root", str(root), "--readme-path", str(root / "README.md"), "--claude-path", str(root / "CLAUDE.md")]) == 0
    assert '"catalog"' in capsys.readouterr().out

    write_text(root / "README.md", "access to 1 agents, 0 skills, and 0 commands\n| Agents | 1 agents |\n| Commands | 0 commands |\n| Skills | 0 skills |\n")
    assert catalog_check.main(["--root", str(root), "--readme-path", str(root / "README.md"), "--claude-path", str(root / "CLAUDE.md")]) == 1

    assert catalog_check.main(["--root", str(root), "--readme-path", str(root / "missing.md"), "--claude-path", str(root / "CLAUDE.md")]) == 1
    assert "エラー:" in capsys.readouterr().err

    assert catalog_check.main(["--root", str(root), "--readme-path", str(root / "README.md"), "--claude-path", str(root / "CLAUDE.md"), "--text"]) == 1
    assert catalog_check.main(["--root", str(root), "--readme-path", str(root / "README.md"), "--claude-path", str(root / "CLAUDE.md"), "--md"]) == 1


def test_catalog_check_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    for folder in ("agents", "commands", "skills"):
        (root / folder).mkdir()
    write_text(
        root / "README.md",
        "access to 0 agents, 0 skills, and 0 commands\n| Agents | 0 agents |\n| Commands | 0 commands |\n| Skills | 0 skills |\n",
    )
    write_text(
        root / "CLAUDE.md",
        "- **agents/** - 0 個の専門サブエージェント\n- **skills/** - 0 個のワークフロー定義とドメイン知識\n- **commands/** - 0 個のスラッシュコマンド\n",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "catalog_check.py",
            "--root",
            str(root),
            "--readme-path",
            str(root / "README.md"),
            "--claude-path",
            str(root / "CLAUDE.md"),
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.ci.catalog_check", run_name="__main__")

    assert excinfo.value.code == 0
