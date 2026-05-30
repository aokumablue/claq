"""status / import / export / prune / projects サブコマンド。

``detect_project`` / ``load_all_instincts`` / ``_fetch_url`` /
``_load_instincts_from_dir`` など ``cli`` 名前空間で ``monkeypatch``
差し替えされる対象は ``_pkg`` 経由で呼び出す。
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import deepblue.skills.learn.cli as _pkg

from .paths import (
    PENDING_EXPIRY_WARNING_DAYS,
    PENDING_TTL_DAYS,
    _project_dir_for_id,
    _validate_file_path,
    _yaml_quote,
)
from .instincts import (
    _print_instincts_by_domain,
    load_project_only_instincts,
    parse_instinct_file,
)
from .pending import _collect_pending_instincts
from .registry import load_registry


# ─────────────────────────────────────────────
# status サブコマンド
# ─────────────────────────────────────────────


def cmd_status(args) -> int:
    """すべての instinct の状態（プロジェクト + グローバル）を表示する。"""
    project = _pkg.detect_project()
    instincts = _pkg.load_all_instincts(project)
    SEP = "=" * 60
    sep = "-" * 60

    if not instincts:
        print("No instincts found.")
        print(f"\nProject: {project['name']} ({project['id']})")
        print(f"  Project instincts:  {project['instincts_personal']}")
        print(f"  Global instincts:   {_pkg.GLOBAL_PERSONAL_DIR}")
    else:
        # スコープごとに分割
        project_instincts = [i for i in instincts if i.get("_scope_label") == "project"]
        global_instincts = [i for i in instincts if i.get("_scope_label") == "global"]

        # ヘッダーを表示
        print(f"\n{SEP}")
        print(f"  INSTINCT STATUS - {len(instincts)} total")
        print(f"{SEP}\n")

        print(f"  Project:  {project['name']} ({project['id']})")
        print(f"  Project instincts: {len(project_instincts)}")
        print(f"  Global instincts:  {len(global_instincts)}")
        print()

        # プロジェクトスコープ instinct を表示
        if project_instincts:
            print(f"## PROJECT-SCOPED ({project['name']})")
            print()
            _print_instincts_by_domain(project_instincts)

        # グローバル instinct を表示
        if global_instincts:
            print("## GLOBAL (apply to all projects)")
            print()
            _print_instincts_by_domain(global_instincts)

        # 観測データの統計
        obs_file = project.get("observations_file")
        if obs_file and Path(obs_file).exists():
            with open(obs_file, encoding="utf-8") as f:
                obs_count = sum(1 for _ in f)
            print(sep)
            print(f"  Observations: {obs_count} events logged")
            print(f"  File: {obs_file}")

    # 保留中 instinct の統計
    pending = _collect_pending_instincts()
    if pending:
        print(f"\n{sep}")
        print(f"  Pending instincts: {len(pending)} awaiting review")

        if len(pending) >= 5:
            print(
                f"\n  ⚠ {len(pending)} pending instincts awaiting review."
                f" Unreviewed instincts auto-delete after {PENDING_TTL_DAYS} days."
            )

        # PENDING_EXPIRY_WARNING_DAYS 以内に期限切れになる instinct を表示
        expiry_threshold = PENDING_TTL_DAYS - PENDING_EXPIRY_WARNING_DAYS
        expiring_soon = [p for p in pending if p["age_days"] >= expiry_threshold and p["age_days"] < PENDING_TTL_DAYS]
        if expiring_soon:
            print(f"\n  Expiring within {PENDING_EXPIRY_WARNING_DAYS} days:")
            for item in expiring_soon:
                days_left = max(0, PENDING_TTL_DAYS - item["age_days"])
                print(f"    - {item['name']} ({days_left}d remaining)")

    print(f"\n{SEP}\n")
    return 0


# ─────────────────────────────────────────────
# import サブコマンド
# ─────────────────────────────────────────────


def cmd_import(args) -> int:
    """ファイルまたは URL から instinct を取り込む。"""
    project = _pkg.detect_project()
    source = args.source

    # 取り込み先スコープを決定
    target_scope = args.scope or "project"
    if target_scope == "project" and project["id"] == "global":
        print("No project detected. Importing as global scope.")
        target_scope = "global"

    # コンテンツを取得
    if source.startswith("http://") or source.startswith("https://"):
        print(f"Fetching from URL: {source}")
        try:
            content = _pkg._fetch_url(source)
        except ValueError as e:
            print(f"Invalid URL: {e}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error fetching URL: {e}", file=sys.stderr)
            return 1
    else:
        try:
            path = _validate_file_path(source, must_exist=True)
        except ValueError as e:
            print(f"Invalid path: {e}", file=sys.stderr)
            return 1
        if not path.is_file():
            print(f"Error: '{path}' is not a regular file.", file=sys.stderr)
            return 1
        content = path.read_text(encoding="utf-8")

    # instinct を解析
    new_instincts = parse_instinct_file(content)
    if not new_instincts:
        print("No valid instincts found in source.")
        return 1

    print(f"\nFound {len(new_instincts)} instincts to import.")
    print(f"Target scope: {target_scope}")
    if target_scope == "project":
        print(f"Target project: {project['name']} ({project['id']})")
    print()

    # 重複判定のため既存 instinct を読み込む（対象スコープのみに限定）
    # （project が global を隠す、またはその逆の）スコープまたぎのシャドーイングを避ける
    if target_scope == "global":
        existing = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
        existing += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
    else:
        existing = load_project_only_instincts(project)
    existing_ids = {i.get("id") for i in existing}

    # 取り込み元内で重複排除: ID ごとに信頼度最大を採用
    best_by_id = {}
    for inst in new_instincts:
        inst_id = inst.get("id")
        if inst_id not in best_by_id or inst.get("confidence", 0.5) > best_by_id[inst_id].get("confidence", 0.5):
            best_by_id[inst_id] = inst
    deduped_instincts = list(best_by_id.values())

    # ディスク上の既存 instinct と照合して分類
    to_add = []
    duplicates = []
    to_update = []

    for inst in deduped_instincts:
        inst_id = inst.get("id")
        if inst_id in existing_ids:
            existing_inst = next((e for e in existing if e.get("id") == inst_id), None)
            if existing_inst:
                if inst.get("confidence", 0) > existing_inst.get("confidence", 0):
                    to_update.append(inst)
                else:
                    duplicates.append(inst)
        else:
            to_add.append(inst)

    # 最小信頼度で絞り込み
    min_conf = args.min_confidence if args.min_confidence is not None else 0.0
    to_add = [i for i in to_add if i.get("confidence", 0.5) >= min_conf]
    to_update = [i for i in to_update if i.get("confidence", 0.5) >= min_conf]

    # サマリーを表示
    if to_add:
        print(f"NEW ({len(to_add)}):")
        for inst in to_add:
            print(f"  + {inst.get('id')} (confidence: {inst.get('confidence', 0.5):.2f})")

    if to_update:
        print(f"\nUPDATE ({len(to_update)}):")
        for inst in to_update:
            print(f"  ~ {inst.get('id')} (confidence: {inst.get('confidence', 0.5):.2f})")

    if duplicates:
        print(f"\nSKIP ({len(duplicates)} - already exists with equal/higher confidence):")
        for inst in duplicates[:5]:
            print(f"  - {inst.get('id')}")
        if len(duplicates) > 5:
            print(f"  ... and {len(duplicates) - 5} more")

    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    if not to_add and not to_update:
        print("\nNothing to import.")
        return 0

    # 確認
    if not args.force:
        response = input(f"\nImport {len(to_add)} new, update {len(to_update)}? [y/N] ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    # スコープに応じて出力ディレクトリを決定
    if target_scope == "global":
        output_dir = _pkg.GLOBAL_INHERITED_DIR
    else:
        output_dir = project["instincts_inherited"]

    output_dir.mkdir(parents=True, exist_ok=True)

    # 更新対象 instinct の古いファイルを収集（新ファイル書き込み後に削除）
    # 重複防止のため、対象スコープ内の任意サブディレクトリ（personal/ または inherited/）から削除を許可
    # 同一 ID が両方に存在しないようにする。さらに以下を防ぐ:
    # scope の instincts ルートに限定してスコープまたぎ削除を防止
    if target_scope == "global":
        scope_root = _pkg.GLOBAL_INSTINCTS_DIR.resolve()
    else:
        scope_root = (
            (project["project_dir"] / "instincts").resolve()
            if project["id"] != "global"
            else _pkg.GLOBAL_INSTINCTS_DIR.resolve()
        )
    stale_paths = []
    for inst in to_update:
        inst_id = inst.get("id")
        stale = next((e for e in existing if e.get("id") == inst_id), None)
        if stale and stale.get("_source_file"):
            stale_path = Path(stale["_source_file"]).resolve()
            if stale_path.exists() and str(stale_path).startswith(str(scope_root) + os.sep):
                stale_paths.append(stale_path)

    # 先に新ファイルを書き込む（失敗時も古いファイルは保持され安全）
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    source_name = Path(source).stem if not source.startswith("http") else "web-import"
    output_file = output_dir / f"{source_name}-{timestamp}.yaml"

    all_to_write = to_add + to_update
    output_content = f"# imported from {source}\n# Date: {datetime.now().isoformat()}\n# Scope: {target_scope}\n"
    if target_scope == "project":
        output_content += f"# Project: {project['name']} ({project['id']})\n"
    output_content += "\n"

    for inst in all_to_write:
        output_content += "---\n"
        output_content += f"id: {inst.get('id')}\n"
        output_content += f"trigger: {_yaml_quote(inst.get('trigger', 'unknown'))}\n"
        output_content += f"confidence: {inst.get('confidence', 0.5)}\n"
        output_content += f"domain: {inst.get('domain', 'general')}\n"
        output_content += "source: inherited\n"
        output_content += f"scope: {target_scope}\n"
        output_content += f"imported_from: {_yaml_quote(source)}\n"
        if target_scope == "project":
            output_content += f"project_id: {project['id']}\n"
            output_content += f"project_name: {project['name']}\n"
        if inst.get("source_repo"):
            output_content += f"source_repo: {inst.get('source_repo')}\n"
        output_content += "---\n\n"
        output_content += inst.get("content", "") + "\n\n"

    output_file.write_text(output_content, encoding="utf-8")

    # 新ファイルの書き込み成功後にのみ古いファイルを削除
    for stale_path in stale_paths:
        try:
            stale_path.unlink()
        except OSError:
            pass  # 削除はベストエフォートで実施

    print("\nImport complete!")
    print(f"   Scope: {target_scope}")
    print(f"   Added: {len(to_add)}")
    print(f"   Updated: {len(to_update)}")
    print(f"   Saved to: {output_file}")

    return 0


# ─────────────────────────────────────────────
# export サブコマンド
# ─────────────────────────────────────────────


def cmd_export(args) -> int:
    """instinct をファイルへ書き出す。"""
    project = _pkg.detect_project()

    # スコープフィルタに基づいて出力対象を決定
    if args.scope == "project":
        instincts = load_project_only_instincts(project)
    elif args.scope == "global":
        instincts = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
        instincts += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
    else:
        instincts = _pkg.load_all_instincts(project)

    # 出力先の妥当性は、対象データの有無に関係なく先に確認する
    out_path = None
    if args.output:
        try:
            out_path = _validate_file_path(args.output)
        except ValueError as e:
            print(f"Invalid output path: {e}", file=sys.stderr)
            return 1
        if out_path.is_dir():
            print(f"Error: '{out_path}' is a directory, not a file.", file=sys.stderr)
            return 1

    if not instincts:
        print("No instincts to export.")
        return 1

    # domain 指定時にフィルタ
    if args.domain:
        instincts = [i for i in instincts if i.get("domain") == args.domain]

    # 最小信頼度で絞り込み
    if args.min_confidence is not None:
        instincts = [i for i in instincts if i.get("confidence", 0.5) >= args.min_confidence]

    if not instincts:
        print("No instincts match the criteria.")
        return 1

    # 出力内容を生成
    output = f"# Instincts export\n# Date: {datetime.now().isoformat()}\n# Total: {len(instincts)}\n"
    if args.scope:
        output += f"# Scope: {args.scope}\n"
    if project["id"] != "global":
        output += f"# Project: {project['name']} ({project['id']})\n"
    output += "\n"

    for inst in instincts:
        output += "---\n"
        for key in [
            "id",
            "trigger",
            "confidence",
            "domain",
            "source",
            "scope",
            "project_id",
            "project_name",
            "source_repo",
        ]:
            if inst.get(key):
                value = inst[key]
                if key == "trigger":
                    output += f"{key}: {_yaml_quote(value)}\n"
                else:
                    output += f"{key}: {value}\n"
        output += "---\n\n"
        output += inst.get("content", "") + "\n\n"

    # ファイルまたは標準出力へ出力
    if args.output:
        if out_path is not None:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(output, encoding="utf-8")
            print(f"Exported {len(instincts)} instincts to {out_path}")
    else:
        print(output)

    return 0


# ─────────────────────────────────────────────
# projects サブコマンド
# ─────────────────────────────────────────────


def cmd_projects(args) -> int:
    """既知の全プロジェクトと、それぞれの instinct 数を一覧表示する。"""
    registry = load_registry()

    if not registry:
        print("No projects registered yet.")
        print("Projects are auto-detected when you use the editor in a project directory.")
        return 0

    SEP = "=" * 60
    print(f"\n{SEP}")
    print(f"  KNOWN PROJECTS - {len(registry)} total")
    print(f"{SEP}\n")

    for pid, pinfo in sorted(registry.items(), key=lambda x: x[1].get("last_seen", ""), reverse=True):
        project_dir = _project_dir_for_id(pid)
        personal_dir = project_dir / "instincts" / "personal"
        inherited_dir = project_dir / "instincts" / "inherited"

        personal_count = len(_pkg._load_instincts_from_dir(personal_dir, "personal", "project"))
        inherited_count = len(_pkg._load_instincts_from_dir(inherited_dir, "inherited", "project"))
        obs_file = project_dir / "observations.jsonl"
        if obs_file.exists():
            with open(obs_file, encoding="utf-8") as f:
                obs_count = sum(1 for _ in f)
        else:
            obs_count = 0

        print(f"  {pinfo.get('name', pid)} [{pid}]")
        print(f"    Root: {pinfo.get('root', 'unknown')}")
        if pinfo.get("remote"):
            print(f"    Remote: {pinfo['remote']}")
        print(f"    Instincts: {personal_count} personal, {inherited_count} inherited")
        print(f"    Observations: {obs_count} events")
        print(f"    Last seen: {pinfo.get('last_seen', 'unknown')}")
        print()

    # グローバル統計
    global_personal = len(_pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global"))
    global_inherited = len(_pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global"))
    print("  GLOBAL")
    print(f"    Instincts: {global_personal} personal, {global_inherited} inherited")

    print(f"\n{SEP}\n")
    return 0


# ─────────────────────────────────────────────
# prune サブコマンド
# ─────────────────────────────────────────────


def cmd_prune(args) -> int:
    """TTL しきい値より古い保留 instinct を削除する。"""
    pending = _collect_pending_instincts()

    expired = [p for p in pending if p["age_days"] >= args.max_age]
    remaining = [p for p in pending if p["age_days"] < args.max_age]

    if args.dry_run:
        if not args.quiet:
            if expired:
                print(f"\n[DRY RUN] Would prune {len(expired)} pending instinct(s) older than {args.max_age} days:\n")
                for item in expired:
                    print(f"  - {item['name']} (age: {item['age_days']}d) — {item['path']}")
            else:
                print(f"No pending instincts older than {args.max_age} days.")
            print(f"\nSummary: {len(expired)} would be pruned, {len(remaining)} remaining")
        return 0

    pruned = 0
    pruned_items = []
    for item in expired:
        try:
            item["path"].unlink()
            pruned += 1
            pruned_items.append(item)
        except OSError as e:
            if not args.quiet:
                print(f"Warning: Failed to delete {item['path']}: {e}", file=sys.stderr)

    if not args.quiet:
        if pruned > 0:
            print(f"\nPruned {pruned} pending instinct(s) older than {args.max_age} days.")
            for item in pruned_items:
                print(f"  - {item['name']} (age: {item['age_days']}d)")
        else:
            print(f"No pending instincts older than {args.max_age} days.")
        failed = len(expired) - pruned
        remaining_total = len(remaining) + failed
        print(f"\nSummary: {pruned} pruned, {remaining_total} remaining")

    return 0
