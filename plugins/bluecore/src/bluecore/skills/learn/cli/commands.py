"""status / import / export / prune / projects サブコマンド。

``detect_project`` / ``load_all_instincts`` / ``_fetch_url`` /
``_load_instincts_from_dir`` など ``cli`` 名前空間で ``monkeypatch``
差し替えされる対象は ``_pkg`` 経由で呼び出す。
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import bluecore.skills.learn.cli as _pkg

from .instincts import (
    _print_instincts_by_domain,
    load_project_only_instincts,
    parse_instinct_file,
)
from .paths import (
    PENDING_EXPIRY_WARNING_DAYS,
    PENDING_TTL_DAYS,
    _project_dir_for_id,
    _validate_file_path,
    _yaml_quote,
)
from .pending import _collect_pending_instincts
from .registry import load_registry

# ─────────────────────────────────────────────
# status サブコマンド
# ─────────────────────────────────────────────


def _print_status_instincts(project: dict, instincts: list[dict], sep: str) -> None:
    """インスティンクト一覧とスコープ別の統計を出力する。"""
    project_instincts = [i for i in instincts if i.get("_scope_label") == "project"]
    global_instincts = [i for i in instincts if i.get("_scope_label") == "global"]
    SEP = "=" * 60

    print(f"\n{SEP}")
    print(f"  INSTINCT STATUS - {len(instincts)} total")
    print(f"{SEP}\n")
    print(f"  Project:  {project['name']} ({project['id']})")
    print(f"  Project instincts: {len(project_instincts)}")
    print(f"  Global instincts:  {len(global_instincts)}")
    print()

    if project_instincts:
        print(f"## PROJECT-SCOPED ({project['name']})")
        print()
        _print_instincts_by_domain(project_instincts)

    if global_instincts:
        print("## GLOBAL (apply to all projects)")
        print()
        _print_instincts_by_domain(global_instincts)

    obs_file = project.get("observations_file")
    if obs_file and Path(obs_file).exists():
        with open(obs_file, encoding="utf-8") as f:
            obs_count = sum(1 for _ in f)
        print(sep)
        print(f"  Observations: {obs_count} events logged")
        print(f"  File: {obs_file}")


def _print_pending_summary(pending: list[dict], sep: str) -> None:
    """保留中インスティンクトの件数・警告・期限切れ情報を出力する。"""
    print(f"\n{sep}")
    print(f"  Pending instincts: {len(pending)} awaiting review")

    if len(pending) >= 5:
        print(
            f"\n  ⚠ {len(pending)} pending instincts awaiting review."
            f" Unreviewed instincts auto-delete after {PENDING_TTL_DAYS} days."
        )

    expiry_threshold = PENDING_TTL_DAYS - PENDING_EXPIRY_WARNING_DAYS
    expiring_soon = [p for p in pending if expiry_threshold <= p["age_days"] < PENDING_TTL_DAYS]
    if expiring_soon:
        print(f"\n  Expiring within {PENDING_EXPIRY_WARNING_DAYS} days:")
        for item in expiring_soon:
            days_left = max(0, PENDING_TTL_DAYS - item["age_days"])
            print(f"    - {item['name']} ({days_left}d remaining)")


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
        _print_status_instincts(project, instincts, sep)

    pending = _collect_pending_instincts()
    if pending:
        _print_pending_summary(pending, sep)

    print(f"\n{SEP}\n")
    return 0


# ─────────────────────────────────────────────
# import サブコマンド
# ─────────────────────────────────────────────


def _fetch_import_content(source: str) -> tuple[str | None, int]:
    """URL またはファイルパスからインポート対象のテキストを取得する。

    Returns:
        (content, exit_code) のタプル。エラー時は (None, 1) を返す。
    """
    if source.startswith("http://") or source.startswith("https://"):
        print(f"Fetching from URL: {source}")
        try:
            return _pkg._fetch_url(source), 0
        except ValueError as e:
            print(f"Invalid URL: {e}", file=sys.stderr)
            return None, 1
        except Exception as e:
            print(f"Error fetching URL: {e}", file=sys.stderr)
            return None, 1
    else:
        try:
            path = _validate_file_path(source, must_exist=True)
        except ValueError as e:
            print(f"Invalid path: {e}", file=sys.stderr)
            return None, 1
        if not path.is_file():
            print(f"Error: '{path}' is not a regular file.", file=sys.stderr)
            return None, 1
        return path.read_text(encoding="utf-8"), 0


def _classify_instincts(
    new_instincts: list[dict], existing: list[dict]
) -> tuple[list[dict], list[dict], list[dict]]:
    """新規インポート候補を既存と照合して追加・更新・スキップに分類する。

    Returns:
        (to_add, to_update, duplicates) のタプル。
    """
    existing_ids = {i.get("id") for i in existing}

    # 取り込み元内で重複排除: ID ごとに信頼度最大を採用
    best_by_id: dict = {}
    for inst in new_instincts:
        inst_id = inst.get("id")
        if inst_id not in best_by_id or inst.get("confidence", 0.5) > best_by_id[inst_id].get("confidence", 0.5):
            best_by_id[inst_id] = inst

    to_add, to_update, duplicates = [], [], []
    for inst in best_by_id.values():
        inst_id = inst.get("id")
        if inst_id in existing_ids:
            existing_inst = next((e for e in existing if e.get("id") == inst_id), None)
            if existing_inst and inst.get("confidence", 0) > existing_inst.get("confidence", 0):
                to_update.append(inst)
            else:
                duplicates.append(inst)
        else:
            to_add.append(inst)
    return to_add, to_update, duplicates


def _print_import_summary(to_add: list, to_update: list, duplicates: list) -> None:
    """インポート計画（追加・更新・スキップ）のサマリーを出力する。"""
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


def _build_import_content(
    source: str, target_scope: str, project: dict, all_to_write: list[dict]
) -> str:
    """インポートする instinct 群を YAML テキストとして構築する。"""
    output_content = (
        f"# imported from {source}\n"
        f"# Date: {datetime.now().isoformat()}\n"
        f"# Scope: {target_scope}\n"
    )
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
    return output_content


def _collect_stale_paths(
    to_update: list[dict], existing: list[dict], scope_root: Path
) -> list[Path]:
    """更新対象 instinct の古いファイルパスを収集する（スコープ境界外は除外）。"""
    stale_paths = []
    for inst in to_update:
        inst_id = inst.get("id")
        stale = next((e for e in existing if e.get("id") == inst_id), None)
        if stale and stale.get("_source_file"):
            stale_path = Path(stale["_source_file"]).resolve()
            if stale_path.exists() and str(stale_path).startswith(str(scope_root) + os.sep):
                stale_paths.append(stale_path)
    return stale_paths


@dataclass(frozen=True)
class ImportContext:
    """インポート操作の共通コンテキスト（取り込み元・スコープ・プロジェクト）。"""

    source: str
    target_scope: str
    project: dict


def _write_import_file(
    ctx: ImportContext,
    output_dir: Path,
    to_add: list,
    to_update: list,
    existing: list,
) -> Path:
    """新ファイルを書き込み、古い stale ファイルを削除して保存先パスを返す。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    if ctx.target_scope == "global":
        scope_root = _pkg.GLOBAL_INSTINCTS_DIR.resolve()
    else:
        scope_root = (
            (ctx.project["project_dir"] / "instincts").resolve()
            if ctx.project["id"] != "global"
            else _pkg.GLOBAL_INSTINCTS_DIR.resolve()
        )
    stale_paths = _collect_stale_paths(to_update, existing, scope_root)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    source_name = Path(ctx.source).stem if not ctx.source.startswith("http") else "web-import"
    output_file = output_dir / f"{source_name}-{timestamp}.yaml"

    output_content = _build_import_content(ctx.source, ctx.target_scope, ctx.project, to_add + to_update)
    output_file.write_text(output_content, encoding="utf-8")

    for stale_path in stale_paths:
        try:
            stale_path.unlink()
        except OSError:
            pass  # 削除はベストエフォートで実施
    return output_file


def _resolve_import_scope(args, project: dict) -> str:
    """インポート先スコープを引数とプロジェクト状態から決定する。"""
    target_scope = args.scope or "project"
    if target_scope == "project" and project["id"] == "global":
        print("No project detected. Importing as global scope.")
        target_scope = "global"
    return target_scope


def _load_existing_for_scope(target_scope: str, project: dict) -> list[dict]:
    """インポート先スコープに対応する既存 instinct リストを返す。"""
    if target_scope == "global":
        existing = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
        existing += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
        return existing
    return load_project_only_instincts(project)


def _apply_min_confidence(to_add: list, to_update: list, args) -> tuple[list, list]:
    """最小信頼度フィルタを to_add・to_update に適用する。"""
    min_conf = args.min_confidence if args.min_confidence is not None else 0.0
    return (
        [i for i in to_add if i.get("confidence", 0.5) >= min_conf],
        [i for i in to_update if i.get("confidence", 0.5) >= min_conf],
    )


def _confirm_and_write_import(
    args, ctx: ImportContext, to_add: list, to_update: list, existing: list
) -> int:
    """確認を取り、インポートファイルを書き込んで結果を出力する。"""
    if args.dry_run:
        print("\n[DRY RUN] No changes made.")
        return 0

    if not to_add and not to_update:
        print("\nNothing to import.")
        return 0

    if not args.force:
        response = input(f"\nImport {len(to_add)} new, update {len(to_update)}? [y/N] ")
        if response.lower() != "y":
            print("Cancelled.")
            return 0

    output_dir = _pkg.GLOBAL_INHERITED_DIR if ctx.target_scope == "global" else ctx.project["instincts_inherited"]
    output_file = _write_import_file(ctx, output_dir, to_add, to_update, existing)

    print("\nImport complete!")
    print(f"   Scope: {ctx.target_scope}")
    print(f"   Added: {len(to_add)}")
    print(f"   Updated: {len(to_update)}")
    print(f"   Saved to: {output_file}")
    return 0


def cmd_import(args) -> int:
    """ファイルまたは URL から instinct を取り込む。"""
    project = _pkg.detect_project()
    source = args.source
    target_scope = _resolve_import_scope(args, project)

    content, err = _fetch_import_content(source)
    if err:
        return err

    new_instincts = parse_instinct_file(content)  # type: ignore[arg-type]
    if not new_instincts:
        print("No valid instincts found in source.")
        return 1

    print(f"\nFound {len(new_instincts)} instincts to import.")
    print(f"Target scope: {target_scope}")
    if target_scope == "project":
        print(f"Target project: {project['name']} ({project['id']})")
    print()

    existing = _load_existing_for_scope(target_scope, project)
    to_add, to_update, duplicates = _classify_instincts(new_instincts, existing)
    to_add, to_update = _apply_min_confidence(to_add, to_update, args)
    _print_import_summary(to_add, to_update, duplicates)

    ctx = ImportContext(source=source, target_scope=target_scope, project=project)
    return _confirm_and_write_import(args, ctx, to_add, to_update, existing)


# ─────────────────────────────────────────────
# export サブコマンド
# ─────────────────────────────────────────────


def _load_instincts_for_scope(args, project: dict) -> list[dict]:
    """エクスポートスコープに応じた instinct リストを返す。"""
    if args.scope == "project":
        return load_project_only_instincts(project)
    if args.scope == "global":
        result = _pkg._load_instincts_from_dir(_pkg.GLOBAL_PERSONAL_DIR, "personal", "global")
        result += _pkg._load_instincts_from_dir(_pkg.GLOBAL_INHERITED_DIR, "inherited", "global")
        return result
    return _pkg.load_all_instincts(project)


def _build_export_content(instincts: list[dict], args, project: dict) -> str:
    """エクスポート出力用の YAML テキストを生成する。"""
    output = f"# Instincts export\n# Date: {datetime.now().isoformat()}\n# Total: {len(instincts)}\n"
    if args.scope:
        output += f"# Scope: {args.scope}\n"
    if project["id"] != "global":
        output += f"# Project: {project['name']} ({project['id']})\n"
    output += "\n"

    for inst in instincts:
        output += "---\n"
        for key in ["id", "trigger", "confidence", "domain", "source", "scope", "project_id", "project_name", "source_repo"]:
            if inst.get(key):
                value = inst[key]
                output += f"{key}: {_yaml_quote(value)}\n" if key == "trigger" else f"{key}: {value}\n"
        output += "---\n\n"
        output += inst.get("content", "") + "\n\n"
    return output


def cmd_export(args) -> int:
    """instinct をファイルへ書き出す。"""
    project = _pkg.detect_project()

    # 出力先の妥当性はデータの有無に関係なく先に確認する
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

    instincts = _load_instincts_for_scope(args, project)
    if not instincts:
        print("No instincts to export.")
        return 1

    if args.domain:
        instincts = [i for i in instincts if i.get("domain") == args.domain]
    if args.min_confidence is not None:
        instincts = [i for i in instincts if i.get("confidence", 0.5) >= args.min_confidence]

    if not instincts:
        print("No instincts match the criteria.")
        return 1

    output = _build_export_content(instincts, args, project)

    # args.output が truthy のとき out_path は上で必ず設定済み（None なら早期 return 済み）。
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


def _prune_dry_run_report(expired: list[dict], remaining: list[dict], max_age: int, quiet: bool) -> None:
    """dry-run モードでの削除予定サマリーを出力する。"""
    if quiet:
        return
    if expired:
        print(f"\n[DRY RUN] Would prune {len(expired)} pending instinct(s) older than {max_age} days:\n")
        for item in expired:
            print(f"  - {item['name']} (age: {item['age_days']}d) — {item['path']}")
    else:
        print(f"No pending instincts older than {max_age} days.")
    print(f"\nSummary: {len(expired)} would be pruned, {len(remaining)} remaining")


def _prune_execute(expired: list[dict], remaining: list[dict], max_age: int, quiet: bool) -> None:
    """期限切れの保留 instinct ファイルを削除し、結果を出力する。"""
    pruned = 0
    pruned_items = []
    for item in expired:
        try:
            item["path"].unlink()
            pruned += 1
            pruned_items.append(item)
        except OSError as e:
            if not quiet:
                print(f"Warning: Failed to delete {item['path']}: {e}", file=sys.stderr)

    if not quiet:
        if pruned > 0:
            print(f"\nPruned {pruned} pending instinct(s) older than {max_age} days.")
            for item in pruned_items:
                print(f"  - {item['name']} (age: {item['age_days']}d)")
        else:
            print(f"No pending instincts older than {max_age} days.")
        failed = len(expired) - pruned
        remaining_total = len(remaining) + failed
        print(f"\nSummary: {pruned} pruned, {remaining_total} remaining")


def cmd_prune(args) -> int:
    """TTL しきい値より古い保留 instinct を削除する。"""
    pending = _collect_pending_instincts()

    expired = [p for p in pending if p["age_days"] >= args.max_age]
    remaining = [p for p in pending if p["age_days"] < args.max_age]

    if args.dry_run:
        _prune_dry_run_report(expired, remaining, args.max_age, args.quiet)
        return 0

    _prune_execute(expired, remaining, args.max_age, args.quiet)
    return 0
