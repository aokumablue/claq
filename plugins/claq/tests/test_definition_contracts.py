"""agents/skills/commands の定義どうしで一致していなければならない文言・一覧を検証する構造テスト。

定義 md は互いを import しないため、片方だけ書き換えると食い違いが実行時まで残る。
停止文言・委譲元の一覧・返却形式の欄・モード指定・相対パスの解決手順を、定義の実体から突き合わせる。
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")

_STOP_PHRASE = (
    "次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告"
)
_STOP_FRAGMENTS = tuple(_STOP_PHRASE.split("・"))
_DEFENSE_POINTER = "報告の「追加した防御」"
_DEFENSE_FIELD_RE = re.compile(r"^(?:## )?追加した防御|`追加した防御`", re.M)
_PLANNER_MODE_RE = re.compile(r"mode: (?:evaluate|decide)")
_RELATIVE_MD_HINT = '`. "$HOME/.claq/env.sh" && claq_plugin_root`'
_DELEGATORS_LINE_RE = re.compile(r"^- `loop-dev` ← (.+)$", re.M)
_DESCRIPTION_DELEGATORS_RE = re.compile(r"^description: .*?。([a-z/-]+) からの委譲専用。$", re.M)
_SKILL_INVOCATION_RE = re.compile(r"`([a-z-]+)` skill")


def _corpus() -> list[tuple[Path, str]]:
    """定義 md の相対パスと本文の組を列挙する。"""
    return [
        (path.relative_to(_ROOT), path.read_text(encoding="utf-8"))
        for dirname in _TARGET_DIRS
        for path in sorted((_ROOT / dirname).rglob("*.md"))
    ]


def _loop_dev_callers() -> set[str]:
    """本文で loop-dev を名指しする command の名前を返す。"""
    return {
        path.stem
        for path in (_ROOT / "commands").glob("*.md")
        if "`loop-dev`" in path.read_text(encoding="utf-8")
    }


def test_stop_phrase_is_identical_everywhere() -> None:
    """停止 4 種文言の一部を書く定義は、4 種を同じ順・同じ文言で書く。"""
    partial = [
        str(path)
        for path, text in _corpus()
        if any(fragment in text for fragment in _STOP_FRAGMENTS) and _STOP_PHRASE not in text
    ]
    assert not partial, "停止文言が正本と一致しない:\n" + "\n".join(partial)


def test_commands_with_stop_section_use_stop_phrase() -> None:
    """「## 停止点」節を持つ command は停止 4 種文言を含む。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if path.parts[0] == "commands" and "## 停止点" in text and _STOP_PHRASE not in text
    ]
    assert not missing, "停止点節に停止文言が無い:\n" + "\n".join(missing)


def test_loop_dev_delegators_match_commands() -> None:
    """loop-dev の description と release-verify の委譲元一覧は、loop-dev を起動する command と一致する。"""
    callers = _loop_dev_callers()
    description = (_ROOT / "skills" / "loop-dev" / "SKILL.md").read_text(encoding="utf-8")
    listed = (_ROOT / "skills" / "release-verify" / "SKILL.md").read_text(encoding="utf-8")
    described = _DESCRIPTION_DELEGATORS_RE.search(description)
    delegators = _DELEGATORS_LINE_RE.search(listed)
    assert described is not None
    assert delegators is not None
    assert set(described.group(1).split("/")) == callers
    assert set(re.findall(r"`([a-z-]+)`", delegators.group(1))) == callers


def test_agents_have_no_parent_relative_md_reference() -> None:
    """agents の本文は `../` の md 参照を持たない（サブエージェントは cwd で始まり解決できない）。"""
    offenders = [
        f"{path}: {ref}"
        for path, text in _corpus()
        if path.parts[0] == "agents"
        for ref in re.findall(r"`(\.\./[^`\n]+\.md)`", text)
    ]
    assert not offenders, "agents に親ディレクトリ相対の md 参照がある:\n" + "\n".join(offenders)


def test_defense_pointer_has_matching_report_field() -> None:
    """報告の「追加した防御」へ残すよう求める定義は、返却形式にその欄を持つ。"""
    missing = [
        str(path) for path, text in _corpus() if _DEFENSE_POINTER in text and not _DEFENSE_FIELD_RE.search(text)
    ]
    assert not missing, "「追加した防御」欄が返却形式に無い:\n" + "\n".join(missing)


def test_planner_callers_state_mode() -> None:
    """claq:planner を起動する定義は、依頼文へ書く `mode:` を明記する。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if "`claq:planner` を起動" in text and not _PLANNER_MODE_RE.search(text)
    ]
    assert not missing, "planner の起動に mode 指定が無い:\n" + "\n".join(missing)


def test_commands_with_relative_md_reference_state_base() -> None:
    """`../` の md 参照を持つ command は、基準ディレクトリの求め方を書く（command の読み込みに基準は付かない）。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if path.parts[0] == "commands" and re.search(r"`\.\./[^`\n]+\.md`", text) and _RELATIVE_MD_HINT not in text
    ]
    assert not missing, "相対 md 参照の基準が書かれていない:\n" + "\n".join(missing)


def test_forked_skills_invoked_by_commands_run_in_foreground() -> None:
    """command が名指しして起動する fork の skill は `background: false` を持つ（既定は後のターンに結果が届く）。"""
    invoked = {
        name
        for path, text in _corpus()
        if path.parts[0] == "commands"
        for name in _SKILL_INVOCATION_RE.findall(text)
    }
    assert invoked, "Skill ツールで skill を起動する command が見つからない"
    missing = [
        name
        for name in sorted(invoked)
        if "\ncontext: fork\n" in (text := (_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8"))
        and "\nbackground: false\n" not in text
    ]
    assert not missing, "fork の skill に background: false が無い:\n" + "\n".join(missing)
