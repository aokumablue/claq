"""スキル棚卸しの CLI エントリポイント。

サブコマンド:
  scan [--project-dir DIR]  スキルインベントリを JSON で出力する
  diff RESULTS_JSON [--project-dir DIR]  変更済み/新規スキルを JSON で出力する
  save RESULTS_JSON         stdin の評価 JSON を results.json にマージ保存する
"""

import argparse
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from bluecore.skills.stocktake import core
from bluecore.skills.stocktake import io as sio


def _resolve_dirs(project_dir_arg: str | None) -> tuple[Path, Path, Path]:
    """環境変数とコマンド引数からグローバル・プロジェクト・観測ファイルのパスを解決する。"""
    global_dir = Path(
        os.environ.get("SKILL_STOCKTAKE_GLOBAL_DIR", Path.home() / ".claude" / "skills")
    )
    project_dir = Path(
        os.environ.get(
            "SKILL_STOCKTAKE_PROJECT_DIR",
            project_dir_arg or (Path.cwd() / ".claude" / "skills"),
        )
    )
    obs_file = Path(
        os.environ.get(
            "SKILL_STOCKTAKE_OBSERVATIONS",
            Path.home() / ".claude" / "observations.jsonl",
        )
    )
    return global_dir, project_dir, obs_file


def _scan_dir(directory: Path, obs: dict) -> list[dict]:
    """ディレクトリ配下のスキルを走査し、メタデータと使用統計の dict 一覧を返す。"""
    skills = []
    home = Path.home()
    for f in sio.walk_skills(directory):
        name, desc = core.parse_frontmatter(f)
        mtime_sec = int(f.stat().st_mtime)
        mtime_str = datetime.fromtimestamp(mtime_sec, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            dp = "~/" + str(f.relative_to(home))
        except ValueError:
            dp = str(f)
        u7, u30 = obs.get(str(f), (0, 0))
        skills.append(
            {
                "path": dp,
                "name": name,
                "description": desc,
                "use_7d": u7,
                "use_30d": u30,
                "mtime": mtime_str,
            }
        )
    return skills


def _cmd_scan(args: argparse.Namespace) -> int:
    """スキルインベントリを JSON で標準出力する。"""
    global_dir, project_dir, obs_file = _resolve_dirs(args.project_dir)

    now = datetime.now(UTC)
    cutoff_7d = now - timedelta(days=7)
    cutoff_30d = now - timedelta(days=30)
    obs = core.aggregate_observations(obs_file, cutoff_7d, cutoff_30d)

    global_skills = _scan_dir(global_dir, obs) if global_dir.is_dir() else []
    project_skills = _scan_dir(project_dir, obs) if project_dir.is_dir() else []

    result = {
        "scan_summary": {
            "global": {"found": global_dir.is_dir(), "count": len(global_skills)},
            "project": {
                "found": project_dir.is_dir(),
                "path": str(project_dir) if project_dir.is_dir() else "",
                "count": len(project_skills),
            },
        },
        "skills": global_skills + project_skills,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    """変更済み/新規スキルを JSON で標準出力する。"""
    results_path = Path(args.results_json)
    if not results_path.is_file():
        print(f"Error: RESULTS_JSON not found: {results_path}", file=sys.stderr)
        return 1

    existing = sio.read_results(results_path)
    # read_results は is_file() 確認後に呼ぶためここでは常に dict
    assert existing is not None

    ea_raw = existing.get("evaluated_at", "")
    try:
        core.validate_evaluated_at(ea_raw)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    evaluated_at = datetime.strptime(ea_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    known_paths: set[str] = set(existing.get("skills", {}).keys())

    global_dir, project_dir, _ = _resolve_dirs(args.project_dir)
    skill_files = sio.walk_skills(global_dir) + sio.walk_skills(project_dir)

    changed = core.classify_changed(known_paths, evaluated_at, skill_files)
    print(json.dumps(changed, indent=2, ensure_ascii=False))
    return 0


def _cmd_save(args: argparse.Namespace) -> int:
    """stdin の評価 JSON を results.json にマージして保存する。"""
    results_path = Path(args.results_json)

    raw = sys.stdin.read()
    try:
        new_data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Error: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 1

    if not isinstance(new_data, dict):
        print("Error: stdin JSON must be an object", file=sys.stderr)
        return 1

    existing = sio.read_results(results_path)
    now = datetime.now(UTC)
    merged = sio.merge_results(existing, new_data, now)
    sio.atomic_write(results_path, merged)
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI エントリポイント。"""
    parser = argparse.ArgumentParser(
        prog="bluecore.skills.stocktake.cli",
        description="スキル棚卸しツール",
    )
    subparsers = parser.add_subparsers(dest="command")

    scan_p = subparsers.add_parser("scan", help="スキルインベントリを出力する")
    scan_p.add_argument("--project-dir", default=None, help="プロジェクトスキルディレクトリ")

    diff_p = subparsers.add_parser("diff", help="変更済み/新規スキルを出力する")
    diff_p.add_argument("results_json", help="results.json のパス")
    diff_p.add_argument("--project-dir", default=None, help="プロジェクトスキルディレクトリ")

    save_p = subparsers.add_parser("save", help="評価結果を results.json に保存する")
    save_p.add_argument("results_json", help="results.json のパス")

    parsed = parser.parse_args(argv)

    if parsed.command == "scan":
        return _cmd_scan(parsed)
    if parsed.command == "diff":
        return _cmd_diff(parsed)
    if parsed.command == "save":
        return _cmd_save(parsed)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
