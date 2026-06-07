"""promote サブコマンドと、特定 / 自動昇格の実装。

``detect_project`` / ``_promote_specific`` / ``_promote_auto`` /
``_find_cross_project_instincts`` / ``_load_instincts_from_dir`` は
``cli`` 名前空間で ``monkeypatch`` 差し替えされるため、これらの呼び出しは
``_pkg`` 経由で行う。
"""

import sys
from datetime import UTC, datetime

import bluecore.skills.learn.cli as _pkg

from .instincts import load_project_only_instincts
from .paths import (
    PROMOTE_CONFIDENCE_THRESHOLD,
    PROMOTE_MIN_PROJECTS,
    _validate_instinct_id,
    _yaml_quote,
)


def cmd_promote(args) -> int:
    """プロジェクトスコープの instinct をグローバルスコープへ昇格する。"""
    project = _pkg.detect_project()

    if args.instinct_id:
        # 特定の instinct を昇格
        return _pkg._promote_specific(project, args.instinct_id, args.force, args.dry_run)
    else:
        # 昇格候補を自動検出
        return _pkg._promote_auto(project, args.force, args.dry_run)


def _build_promoted_content(target: dict, project: dict) -> str:
    """昇格対象 instinct の YAML テキストを構築する。"""
    output_content = "---\n"
    output_content += f"id: {target.get('id')}\n"
    output_content += f"trigger: {_yaml_quote(target.get('trigger', 'unknown'))}\n"
    output_content += f"confidence: {target.get('confidence', 0.5)}\n"
    output_content += f"domain: {target.get('domain', 'general')}\n"
    output_content += f"source: {target.get('source', 'promoted')}\n"
    output_content += "scope: global\n"
    output_content += f"promoted_from: {project['id']}\n"
    output_content += f"promoted_date: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}\n"
    output_content += "---\n\n"
    output_content += target.get("content", "") + "\n"
    return output_content


def _promote_specific(project: dict, instinct_id: str, force: bool, dry_run: bool = False) -> int:
    """現在のプロジェクトから、指定 ID の instinct をグローバルへ昇格する。"""
    if not _validate_instinct_id(instinct_id):
        print(f"Invalid instinct ID: '{instinct_id}'.", file=sys.stderr)
        return 1

    project_instincts = load_project_only_instincts(project)
    target = next((i for i in project_instincts if i.get("id") == instinct_id), None)

    if not target:
        print(f"Instinct '{instinct_id}' not found in project {project['name']}.")
        return 1

    global_instincts = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
    global_instincts += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
    if any(i.get("id") == instinct_id for i in global_instincts):
        print(f"Instinct '{instinct_id}' already exists in global scope.")
        return 1

    print(f"\nPromoting: {instinct_id}")
    print(f"  From: project '{project['name']}'")
    print(f"  Confidence: {target.get('confidence', 0.5):.0%}")
    print(f"  Domain: {target.get('domain', 'general')}")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    if not force:
        response = input("\nPromote to global? [y/N] ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    output_file = _pkg.GLOBAL_PERSONAL_DIR / f"{instinct_id}.yaml"
    output_file.write_text(_build_promoted_content(target, project), encoding="utf-8")
    print(f"\nPromoted '{instinct_id}' to global scope.")
    print(f"  Saved to: {output_file}")
    return 0


def _build_auto_promoted_content(inst: dict, avg_confidence: float, n_projects: int) -> str:
    """自動昇格 instinct の YAML テキストを構築する。"""
    output_content = "---\n"
    output_content += f"id: {inst.get('id')}\n"
    output_content += f"trigger: {_yaml_quote(inst.get('trigger', 'unknown'))}\n"
    output_content += f"confidence: {avg_confidence}\n"
    output_content += f"domain: {inst.get('domain', 'general')}\n"
    output_content += "source: auto-promoted\n"
    output_content += "scope: global\n"
    output_content += f"promoted_date: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}\n"
    output_content += f"seen_in_projects: {n_projects}\n"
    output_content += "---\n\n"
    output_content += inst.get("content", "") + "\n"
    return output_content


def _collect_auto_candidates(cross: dict, global_ids: set) -> list[dict]:
    """クロスプロジェクト instinct から自動昇格候補を抽出する。"""
    candidates = []
    for iid, entries in cross.items():
        if iid in global_ids:
            continue
        avg_conf = sum(e[2].get("confidence", 0.5) for e in entries) / len(entries)
        if avg_conf >= PROMOTE_CONFIDENCE_THRESHOLD and len(entries) >= PROMOTE_MIN_PROJECTS:
            candidates.append({"id": iid, "entries": entries, "avg_confidence": avg_conf})
    return candidates


def _write_auto_promoted(candidates: list) -> int:
    """候補リストをグローバルスコープへ書き込み、昇格件数を返す。"""
    promoted = 0
    for cand in candidates:
        if not _validate_instinct_id(cand["id"]):
            print(f"Skipping invalid instinct ID during promotion: {cand['id']}", file=sys.stderr)
            continue
        best_entry = max(cand["entries"], key=lambda e: e[2].get("confidence", 0.5))
        inst = best_entry[2]
        output_file = _pkg.GLOBAL_PERSONAL_DIR / f"{cand['id']}.yaml"
        output_file.write_text(
            _build_auto_promoted_content(inst, cand["avg_confidence"], len(cand["entries"])),
            encoding="utf-8",
        )
        promoted += 1
    return promoted


def _promote_auto(project: dict, force: bool, dry_run: bool) -> int:
    """複数プロジェクトで見つかった instinct を自動昇格する。"""
    cross = _pkg._find_cross_project_instincts()

    global_instincts = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
    global_instincts += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
    global_ids = {i.get("id") for i in global_instincts}

    candidates = _collect_auto_candidates(cross, global_ids)

    if not candidates:
        print("No instincts qualify for auto-promotion.")
        print(f"  Criteria: appears in {PROMOTE_MIN_PROJECTS}+ projects, avg confidence >= {PROMOTE_CONFIDENCE_THRESHOLD:.0%}")
        return 0

    print(f"\n{'=' * 60}")
    print(f"  AUTO-PROMOTION CANDIDATES - {len(candidates)} found")
    print(f"{'=' * 60}\n")

    for cand in candidates:
        proj_names = ", ".join(pname for _, pname, _ in cand["entries"])
        print(f"  {cand['id']} (avg: {cand['avg_confidence']:.0%})")
        print(f"    Found in {len(cand['entries'])} projects: {proj_names}")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    if not force:
        response = input(f"\nPromote {len(candidates)} instincts to global? [y/N] ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    promoted = _write_auto_promoted(candidates)
    print(f"\nPromoted {promoted} instincts to global scope.")
    return 0
