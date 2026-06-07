"""evolve サブコマンドと昇格候補の検出・evolved 構造生成。

``detect_project`` / ``load_all_instincts`` / ``_generate_evolved`` /
``_show_promotion_candidates`` / ``_find_cross_project_instincts`` /
``_load_instincts_from_dir`` は ``cli`` 名前空間で ``monkeypatch`` 差し替え
されるため、これらの呼び出しは ``_pkg`` 経由で行う。
"""

import re
from collections import defaultdict
from pathlib import Path

import bluecore.skills.learn.cli as _pkg

from .paths import (
    PROMOTE_CONFIDENCE_THRESHOLD,
    PROMOTE_MIN_PROJECTS,
    _project_dir_for_id,
)
from .registry import load_registry


def _build_trigger_clusters(instincts: list) -> list[dict]:
    """instinct リストから trigger が類似するクラスタを抽出してソート済みリストを返す。"""
    trigger_clusters: dict = defaultdict(list)
    for inst in instincts:
        trigger_key = inst.get("trigger", "").lower()
        for keyword in ["when", "creating", "writing", "adding", "implementing", "testing"]:
            trigger_key = trigger_key.replace(keyword, "").strip()
        trigger_clusters[trigger_key].append(inst)

    skill_candidates = []
    for trigger, cluster in trigger_clusters.items():
        if len(cluster) >= 2:
            avg_conf = sum(i.get("confidence", 0.5) for i in cluster) / len(cluster)
            skill_candidates.append(
                {
                    "trigger": trigger,
                    "instincts": cluster,
                    "avg_confidence": avg_conf,
                    "domains": list({i.get("domain", "general") for i in cluster}),
                    "scopes": list({i.get("scope", "project") for i in cluster}),
                }
            )
    skill_candidates.sort(key=lambda x: (-len(x["instincts"]), -x["avg_confidence"]))
    return skill_candidates


def _print_skill_candidates(skill_candidates: list) -> None:
    """スキル候補クラスタを最大 5 件表示する。"""
    if not skill_candidates:
        return
    print("\n## SKILL CANDIDATES\n")
    for i, cand in enumerate(skill_candidates[:5], 1):
        scope_info = ", ".join(cand["scopes"])
        print(f'{i}. Cluster: "{cand["trigger"]}"')
        print(f"   Instincts: {len(cand['instincts'])}")
        print(f"   Avg confidence: {cand['avg_confidence']:.0%}")
        print(f"   Domains: {', '.join(cand['domains'])}")
        print(f"   Scopes: {scope_info}")
        print("   Instincts:")
        for inst in cand["instincts"][:3]:
            print(f"     - {inst.get('id')} [{inst.get('scope', '?')}]")
        print()


def _print_workflow_candidates(workflow_instincts: list) -> None:
    """ワークフロー系コマンド候補を最大 5 件表示する。"""
    if not workflow_instincts:
        return
    print(f"\n## COMMAND CANDIDATES ({len(workflow_instincts)})\n")
    for inst in workflow_instincts[:5]:
        trigger = inst.get("trigger", "unknown")
        cmd_name = trigger.replace("when ", "").replace("implementing ", "").replace("a ", "")
        cmd_name = cmd_name.replace(" ", "-")[:20]
        print(f"  /{cmd_name}")
        print(f"    From: {inst.get('id')} [{inst.get('scope', '?')}]")
        print(f"    Confidence: {inst.get('confidence', 0.5):.0%}")
        print()


def _print_agent_candidates(agent_candidates: list) -> None:
    """エージェント候補を最大 3 件表示する。"""
    if not agent_candidates:
        return
    print(f"\n## AGENT CANDIDATES ({len(agent_candidates)})\n")
    for cand in agent_candidates[:3]:
        agent_name = cand["trigger"].replace(" ", "-")[:20] + "-agent"
        print(f"  {agent_name}")
        print(f"    Covers {len(cand['instincts'])} instincts")
        print(f"    Avg confidence: {cand['avg_confidence']:.0%}")
        print()


def _handle_generate(args, project: dict, skill_candidates: list, workflow_instincts: list, agent_candidates: list) -> None:
    """--generate フラグが立っている場合に evolved 構造を生成して結果を表示する。"""
    if not args.generate:
        return
    evolved_dir = project["evolved_dir"] if project["id"] != "global" else _pkg.GLOBAL_EVOLVED_DIR
    generated = _pkg._generate_evolved(skill_candidates, workflow_instincts, agent_candidates, evolved_dir)
    if generated:
        print(f"\nGenerated {len(generated)} evolved structures:")
        for path in generated:
            print(f"   {path}")
    else:
        print("\nNo structures generated (need higher-confidence clusters).")


def cmd_evolve(args) -> int:
    """instinct を分析し、skill/command/agent への進化候補を提案する。"""
    project = _pkg.detect_project()
    instincts = _pkg.load_all_instincts(project)

    if len(instincts) < 3:
        print("Need at least 3 instincts to analyze patterns.")
        print(f"Currently have: {len(instincts)}")
        return 1

    project_instincts = [i for i in instincts if i.get("_scope_label") == "project"]
    global_instincts = [i for i in instincts if i.get("_scope_label") == "global"]
    SEP = "=" * 60

    print(f"\n{SEP}")
    print(f"  EVOLVE ANALYSIS - {len(instincts)} instincts")
    print(f"  Project: {project['name']} ({project['id']})")
    print(f"  Project-scoped: {len(project_instincts)} | Global: {len(global_instincts)}")
    print(f"{SEP}\n")

    high_conf = [i for i in instincts if i.get("confidence", 0) >= 0.8]
    print(f"High confidence instincts (>=80%): {len(high_conf)}")

    skill_candidates = _build_trigger_clusters(instincts)
    print(f"\nPotential skill clusters found: {len(skill_candidates)}")

    _print_skill_candidates(skill_candidates)

    workflow_instincts = [i for i in instincts if i.get("domain") == "workflow" and i.get("confidence", 0) >= 0.7]
    _print_workflow_candidates(workflow_instincts)

    agent_candidates = [c for c in skill_candidates if len(c["instincts"]) >= 3 and c["avg_confidence"] >= 0.75]
    _print_agent_candidates(agent_candidates)

    _pkg._show_promotion_candidates(project)
    _handle_generate(args, project, skill_candidates, workflow_instincts, agent_candidates)

    print(f"\n{SEP}\n")
    return 0


def _find_cross_project_instincts() -> dict:
    """複数プロジェクトに現れる instinct（昇格候補）を探す。

    instinct ID をキーに、(project_id, instinct) タプルのリストを値に持つ辞書を返す。
    """
    registry = load_registry()
    cross_project = defaultdict(list)

    for pid, pinfo in registry.items():
        project_dir = _project_dir_for_id(pid)
        personal_dir = project_dir / "instincts" / "personal"
        inherited_dir = project_dir / "instincts" / "inherited"

        # 二重カウント防止のため、このプロジェクトで既出の instinct ID を追跡
        # （例: personal/ と inherited/ の両方にある）同一 instinct を 1 プロジェクト内で重複計上しない
        seen_in_project = set()
        for d, stype in [(personal_dir, "personal"), (inherited_dir, "inherited")]:
            for inst in _pkg._load_instincts_from_dir(d, stype, "project"):
                iid = inst.get("id")
                if iid and iid not in seen_in_project:
                    seen_in_project.add(iid)
                    cross_project[iid].append((pid, pinfo.get("name", pid), inst))

    # 2 つ以上のユニークなプロジェクトに現れるものだけに絞る
    return {iid: entries for iid, entries in cross_project.items() if len(entries) >= 2}


def _show_promotion_candidates(project: dict) -> None:
    """プロジェクトからグローバルへ昇格可能な instinct を表示する。"""
    cross = _pkg._find_cross_project_instincts()

    if not cross:
        return

    # すでに global でない高信頼度のものに絞る
    global_instincts = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
    global_instincts += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
    global_ids = {i.get("id") for i in global_instincts}

    candidates = []
    for iid, entries in cross.items():
        if iid in global_ids:
            continue
        avg_conf = sum(e[2].get("confidence", 0.5) for e in entries) / len(entries)
        if avg_conf >= PROMOTE_CONFIDENCE_THRESHOLD:
            candidates.append(
                {
                    "id": iid,
                    "projects": [(pid, pname) for pid, pname, _ in entries],
                    "avg_confidence": avg_conf,
                    "sample": entries[0][2],
                }
            )

    if candidates:
        print("\n## PROMOTION CANDIDATES (project -> global)\n")
        print(f"  These instincts appear in {PROMOTE_MIN_PROJECTS}+ projects with high confidence:\n")
        for cand in candidates[:10]:
            proj_names = ", ".join(pname for _, pname in cand["projects"])
            print(f"  * {cand['id']} (avg: {cand['avg_confidence']:.0%})")
            print(f"    Found in: {proj_names}")
            print()
        print("  Run `python3 -m bluecore.skills.learn.cli promote` to promote these to global scope.\n")


def _generate_skill_file(cand: dict, evolved_dir: Path) -> str | None:
    """スキル候補から SKILL.md を生成し、生成したパスを返す。スキップ時は None。"""
    trigger = cand["trigger"].strip()
    if not trigger:
        return None
    name = re.sub(r"[^a-z0-9]+", "-", trigger.lower()).strip("-")[:30]
    if not name:
        return None

    skill_dir = evolved_dir / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    content = f"# {name}\n\n"
    content += f"Evolved from {len(cand['instincts'])} instincts "
    content += f"(avg confidence: {cand['avg_confidence']:.0%})\n\n"
    content += "## When to Apply\n\n"
    content += f"Trigger: {trigger}\n\n"
    content += "## Actions\n\n"
    for inst in cand["instincts"]:
        inst_content = inst.get("content", "")
        action_match = re.search(r"## Action\s*\n\s*(.+?)(?:\n\n|\n##|$)", inst_content, re.DOTALL)
        action = action_match.group(1).strip() if action_match else inst.get("id", "unnamed")
        content += f"- {action}\n"

    out = skill_dir / "SKILL.md"
    out.write_text(content, encoding="utf-8")
    return str(out)


def _generate_command_file(inst: dict, evolved_dir: Path) -> str | None:
    """ワークフロー instinct からコマンドファイルを生成し、パスを返す。スキップ時は None。"""
    trigger = inst.get("trigger", "unknown")
    cmd_name = re.sub(r"[^a-z0-9]+", "-", trigger.lower().replace("when ", "").replace("implementing ", ""))
    cmd_name = cmd_name.strip("-")[:20]
    if not cmd_name:
        return None

    cmd_file = evolved_dir / "commands" / f"{cmd_name}.md"
    content = f"# {cmd_name}\n\n"
    content += f"Evolved from instinct: {inst.get('id', 'unnamed')}\n"
    content += f"Confidence: {inst.get('confidence', 0.5):.0%}\n\n"
    content += inst.get("content", "")

    cmd_file.write_text(content, encoding="utf-8")
    return str(cmd_file)


def _generate_agent_file(cand: dict, evolved_dir: Path) -> str | None:
    """エージェント候補からエージェントファイルを生成し、パスを返す。スキップ時は None。"""
    trigger = cand["trigger"].strip()
    agent_name = re.sub(r"[^a-z0-9]+", "-", trigger.lower()).strip("-")[:20]
    if not agent_name:
        return None

    agent_file = evolved_dir / "agents" / f"{agent_name}.md"
    domains = ", ".join(cand["domains"])
    instinct_ids = [i.get("id", "unnamed") for i in cand["instincts"]]

    content = "---\nmodel: sonnet\ntools: Read, Grep, Glob\n---\n"
    content += f"# {agent_name}\n\n"
    content += f"Evolved from {len(cand['instincts'])} instincts "
    content += f"(avg confidence: {cand['avg_confidence']:.0%})\n"
    content += f"Domains: {domains}\n\n"
    content += "## Source Instincts\n\n"
    for iid in instinct_ids:
        content += f"- {iid}\n"

    agent_file.write_text(content, encoding="utf-8")
    return str(agent_file)


def _generate_evolved(
    skill_candidates: list, workflow_instincts: list, agent_candidates: list, evolved_dir: Path
) -> list[str]:
    """分析した instinct クラスタから skill/command/agent ファイルを生成する。"""
    generated = []

    for cand in skill_candidates[:5]:
        path = _generate_skill_file(cand, evolved_dir)
        if path:
            generated.append(path)

    for inst in workflow_instincts[:5]:
        path = _generate_command_file(inst, evolved_dir)
        if path:
            generated.append(path)

    for cand in agent_candidates[:3]:
        path = _generate_agent_file(cand, evolved_dir)
        if path:
            generated.append(path)

    return generated
