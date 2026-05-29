"""skill_create_output モジュールのテスト。"""

from __future__ import annotations

import io
import json
import runpy
import sys

import pytest

from devgear import skill_create_output as sco


def test_formatters_cover_core_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sco, "normalize_git_hosting_service", lambda service: service)
    monkeypatch.setattr(sco, "detect_git_hosting_service", lambda *args, **kwargs: "github")
    monkeypatch.setattr(sco, "get_git_hosting_service_label", lambda service: service.upper())
    monkeypatch.setattr(sco, "get_git_hosting_review_command", lambda service: f"{service}-review")

    assert sco.strip_ansi("\x1b[31mred\x1b[0m") == "red"
    assert "Title" in sco.box("Title", "line 1\nline 2", width=20)
    assert sco.strip_ansi(sco.progress_bar(0)).endswith("0%")
    assert sco.strip_ansi(sco.progress_bar(50)).endswith("50%")
    assert sco.strip_ansi(sco.progress_bar(100)).endswith("100%")
    assert "Repo" in sco.render_header("Repo")
    assert "Commits Analyzed" in sco.render_analysis_results({"commits": 3, "timeRange": "t", "contributors": "c", "files": "f"})
    assert "Key Patterns Discovered" in sco.render_patterns([{"name": "n", "trigger": "t", "confidence": 0.9, "evidence": "e"}])
    assert "Instincts Generated" in sco.render_instincts([{"name": "n", "confidence": 0.5}])
    assert "Generation Complete" in sco.render_output("skill.md", "instincts.md")
    assert "Next Steps" in sco.render_next_steps()
    assert "GitHub App" in sco.render_footer("github")
    assert "gitlab-review" in sco.render_footer("gitlab")
    assert "Analyzing Repository" in sco.render_analyze_phase({"commits": 2})


@pytest.mark.parametrize(
    ("command", "stdin_payload", "expected_fragment"),
    [
        ("header", None, "devgear Skill Creator"),
        ("analysis-results", {"commits": 1}, "Commits Analyzed"),
        ("patterns", [{"name": "alpha"}], "Key Patterns Discovered"),
        ("instincts", [{"name": "beta"}], "Instincts Generated"),
        ("output", {"skillPath": "skill.md", "instinctsPath": "instincts.md"}, "Skill File:"),
        ("next-steps", None, "Next Steps"),
        ("footer", None, "GitHub App"),
        ("analyze-phase", {"commits": 4}, "Analyzing Repository"),
    ],
)
def test_main_commands(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], command: str, stdin_payload: object | None, expected_fragment: str) -> None:
    if stdin_payload is None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    else:
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(stdin_payload)))

    assert sco.main([command] if command != "header" else [command, "Repo"]) == 0
    assert expected_fragment in capsys.readouterr().out


def test_main_help_and_errors(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert sco.main([]) == 0
    assert "Usage:" in capsys.readouterr().out

    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    assert sco.main(["analysis-results"]) == 1
    assert "ERROR:" in capsys.readouterr().err

    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    assert sco.main(["unknown"]) == 1
    assert "Unknown command" in capsys.readouterr().err


def test_color_helpers_and_entrypoint(monkeypatch: pytest.MonkeyPatch) -> None:
    assert sco.white("x").startswith("\x1b[")
    assert sco.red("x").startswith("\x1b[")
    assert sco.bg_cyan("x").startswith("\x1b[")

    monkeypatch.setattr(sys, "argv", ["skill_create_output.py"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("devgear.skill_create_output", run_name="__main__")

    assert excinfo.value.code == 0
