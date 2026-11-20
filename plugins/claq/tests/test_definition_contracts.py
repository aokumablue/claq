"""agents/skills/commands の定義どうしで一致していなければならない文言・一覧を検証する構造テスト。

定義 md は互いを import しないため、片方だけ書き換えると食い違いが実行時まで残る。
停止文言・委譲元の一覧・返却形式の欄・モード指定・相対パスの解決手順を、定義の実体から突き合わせる。
"""

from __future__ import annotations

import re
from pathlib import Path

from claq.ci.ci_common import extract_frontmatter

_ROOT = Path(__file__).resolve().parents[1]
_TARGET_DIRS = ("agents", "skills", "commands")

_STOP_PHRASE = (
    "次の手順を予告して終わる要約・続けてよいかの伺い・作業を止めない判断事項の列挙・区切りや長さを理由にした報告"
)
_STOP_FRAGMENTS = tuple(_STOP_PHRASE.split("・"))
_DEFENSE_POINTER = "報告の「追加した防御」"
_DEFENSE_FIELD_RE = re.compile(r"^(?:## )?追加した防御|`追加した防御`", re.M)
_STOP_HEADING_RE = re.compile(r"^## 停止点$", re.M)
_IMPLEMENTATION_HEADING_RE = re.compile(r"^#{2,3} 実装の手順$", re.M)
_PLANNER_MODE_RE = re.compile(r"mode: (?:evaluate|decide)")
_CANDIDATE_REPORT = "候補を呼び出し元へ報告する"
_CANDIDATE_CONSUME = "学びの候補"
_RELATIVE_MD_HINT ='`. "$HOME/.claq/env.sh" && claq_plugin_root`'
_DELEGATORS_LINE_RE = re.compile(r"^- `loop-dev` ← (.+)$", re.M)
_DESCRIPTION_DELEGATORS_RE = re.compile(r"^description: .*?。([a-z/-]+) からの委譲専用。$", re.M)
_SKILL_INVOCATION_RE = re.compile(r"`([a-z-]+)` skill")
# command が名指しせず、モデルが作る前に自分で起動する fork の skill（判定が次の手順を決める）。
_MODEL_INVOKED_FORKS = frozenset({"search"})


def _corpus() -> list[tuple[Path, str]]:
    """定義 md の相対パスと本文の組を列挙する。"""
    return [
        (path.relative_to(_ROOT), path.read_text(encoding="utf-8"))
        for dirname in _TARGET_DIRS
        for path in sorted((_ROOT / dirname).rglob("*.md"))
    ]


def _is_fork_skill(path: Path, text: str) -> bool:
    """skill の SKILL.md で、frontmatter の `context` が `fork` かを返す。"""
    if path.parts[0] != "skills" or path.name != "SKILL.md":
        return False
    frontmatter = extract_frontmatter(text) or {}
    return frontmatter.get("context") == "fork"


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


def test_stop_section_and_fork_definitions_use_stop_phrase() -> None:
    """「## 停止点」見出しを持つ定義と、frontmatter が `context: fork` の skill は停止 4 種文言を含む。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if (_STOP_HEADING_RE.search(text) or _is_fork_skill(path, text)) and _STOP_PHRASE not in text
    ]
    assert not missing, "停止点節・fork の定義に停止文言が無い:\n" + "\n".join(missing)


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
    """「実装の手順」見出し（## / ###）を持つ定義と、報告の「追加した防御」へ残すよう求める定義は、その欄を持つ。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if (_IMPLEMENTATION_HEADING_RE.search(text) or _DEFENSE_POINTER in text) and not _DEFENSE_FIELD_RE.search(text)
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


def test_callers_of_candidate_reporting_agents_consume_candidates() -> None:
    """学びの候補を呼び出し元へ報告する agent を起動する定義は、その候補を取捨する手順を持つ。

    子は自分では知識カードを書かないため、呼び出し元が拾わなければ候補は失われる。
    """
    reporters = [
        path.stem
        for path, text in _corpus()
        if path.parts[0] == "agents" and _CANDIDATE_REPORT in text
    ]
    assert reporters, "学びの候補を報告する agent が見つからない"
    missing = [
        str(path)
        for path, text in _corpus()
        if path.parts[0] != "agents"
        and any(f"`claq:{name}`" in text for name in reporters)
        and _CANDIDATE_CONSUME not in text
    ]
    assert not missing, "子の学びの候補を取捨する手順が無い:\n" + "\n".join(missing)


def test_tdd_writer_receives_task_type_from_loop_dev() -> None:
    """loop-dev は tdd-writer への依頼文に `task_type` を載せる（tdd-writer は `task_type: test` で経路を変える）。"""
    loop_dev = (_ROOT / "skills" / "loop-dev" / "SKILL.md").read_text(encoding="utf-8")
    tdd_writer = (_ROOT / "agents" / "tdd-writer.md").read_text(encoding="utf-8")
    assert "`task_type: test`" in tdd_writer
    forwarding = [line for line in loop_dev.splitlines() if "`task_type`" in line and "tdd-writer" in line]
    assert forwarding, "loop-dev が tdd-writer へ task_type を渡す行が無い"


def test_commands_with_relative_md_reference_state_base() -> None:
    """`../` の md 参照を持つ command は、基準ディレクトリの求め方を書く（command の読み込みに基準は付かない）。"""
    missing = [
        str(path)
        for path, text in _corpus()
        if path.parts[0] == "commands" and re.search(r"`\.\./[^`\n]+\.md`", text) and _RELATIVE_MD_HINT not in text
    ]
    assert not missing, "相対 md 参照の基準が書かれていない:\n" + "\n".join(missing)


def test_forked_skills_invoked_by_commands_run_in_foreground() -> None:
    """結果が次の手順を決める fork の skill は `background: false` を持つ（既定は後のターンに結果が届く）。"""
    invoked = _MODEL_INVOKED_FORKS | {
        name
        for path, text in _corpus()
        if path.parts[0] == "commands"
        for name in _SKILL_INVOCATION_RE.findall(text)
    }
    assert invoked, "Skill ツールで skill を起動する command が見つからない"
    frontmatters = {
        name: extract_frontmatter((_ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")) or {}
        for name in sorted(invoked)
    }
    missing = [
        name
        for name, frontmatter in frontmatters.items()
        if frontmatter.get("context") == "fork" and frontmatter.get("background") is not False
    ]
    assert not missing, "fork の skill に background: false が無い:\n" + "\n".join(missing)
