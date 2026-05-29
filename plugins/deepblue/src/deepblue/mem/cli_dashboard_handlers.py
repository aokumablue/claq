"""mem CLI: dashboard/import handlers and overview collectors."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from devgear.lib.skill_evolution import collect_skill_health, summarize_health_report

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from devgear.mem.database import Database
    from devgear.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GitUserFn = Callable[[], str]
    CountLinesFn = Callable[[Path], int]


def count_lines(path: Path) -> int:
    """ファイルの行数を数える。"""
    try:
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def collect_project_overview(*, count_lines_fn: CountLinesFn, log: Any) -> dict:
    """既知プロジェクトと instinct の集計を返す。"""
    from devgear.skills.learn.cli import (
        GLOBAL_INHERITED_DIR,
        GLOBAL_PERSONAL_DIR,
        _load_instincts_from_dir,
        _project_dir_for_id,
        load_registry,
    )

    registry = load_registry()
    projects: list[dict[str, object]] = []
    total_personal = 0
    total_inherited = 0

    valid_projects = [(pid, info) for pid, info in registry.items() if isinstance(info, dict)]
    if len(valid_projects) != len(registry):
        log.warning("project registry contains invalid entries; skipping them")

    for project_id, project_info in sorted(
        valid_projects,
        key=lambda item: str(item[1].get("last_seen", "")),
        reverse=True,
    ):
        project_dir = _project_dir_for_id(project_id)
        personal_count = len(_load_instincts_from_dir(project_dir / "instincts" / "personal", "personal", "project"))
        inherited_count = len(_load_instincts_from_dir(project_dir / "instincts" / "inherited", "inherited", "project"))
        total_personal += personal_count
        total_inherited += inherited_count
        projects.append(
            {
                "id": project_id,
                "name": project_info.get("name", project_id),
                "personal_instincts": personal_count,
                "inherited_instincts": inherited_count,
                "observations": count_lines_fn(project_dir / "observations.jsonl"),
                "last_seen": project_info.get("last_seen", "unknown"),
            }
        )

    global_personal = len(_load_instincts_from_dir(GLOBAL_PERSONAL_DIR, "personal", "global"))
    global_inherited = len(_load_instincts_from_dir(GLOBAL_INHERITED_DIR, "inherited", "global"))

    return {
        "projects": projects,
        "summary": {
            "total_projects": len(valid_projects),
            "personal_instincts": total_personal,
            "inherited_instincts": total_inherited,
            "global_personal": global_personal,
            "global_inherited": global_inherited,
        },
    }


def collect_skill_health_overview(options: dict[str, object], *, log: Any) -> dict[str, object]:
    """skill health の集計データを返す。"""
    try:
        report = collect_skill_health(options)
    except Exception as error:  # noqa: BLE001 - ダッシュボードは失敗で止めない
        log.warning("skill health collection failed: %s", error)
        report = {"generated_at": None, "skills": []}

    summary = summarize_health_report(report)
    skills = sorted(
        report.get("skills", []),
        key=lambda skill: (
            not bool(skill.get("declining")),
            -int(skill.get("run_count_30d", 0) or 0),
            str(skill.get("skill_id", "")),
        ),
    )
    display_skills = skills[:20]

    return {
        "report": report,
        "summary": summary,
        "skills": display_skills,
        "chart_labels": [str(skill.get("skill_id", "")) for skill in display_skills],
        "chart_7d": [
            round(float(skill.get("success_rate_7d") or 0) * 100, 1) if skill.get("success_rate_7d") is not None else 0
            for skill in display_skills
        ],
        "chart_30d": [
            round(float(skill.get("success_rate_30d") or 0) * 100, 1) if skill.get("success_rate_30d") is not None else 0
            for skill in display_skills
        ],
    }


def collect_skill_growth_overview(settings: Settings, days: int, *, log: Any) -> dict[str, object]:
    """skill growth の提案データを返す。"""
    sync_cfg = settings.sync
    empty = {
        "summary": {"total_patterns": 0, "total_gaps": 0, "skill_candidates": 0, "gap_candidates": 0},
        "skill_candidates": [],
        "gap_candidates": [],
        "action_items": [],
        "chart_labels": [],
        "chart_scores": [],
    }

    if not sync_cfg.enabled or not sync_cfg.postgres_url:
        return empty

    try:
        from devgear.mem import skill_analyzer, skill_proposal
        from devgear.mem.pg_database import PgDatabase

        pg = PgDatabase(sync_cfg.postgres_url)
        if not pg.test_connection():
            return empty

        try:
            patterns = skill_analyzer.detect_repeated_patterns(pg, min_count=3, days=days)
            gaps = skill_analyzer.detect_skill_gaps(pg, days=days)
            proposal = skill_proposal.generate_proposal(patterns, gaps)
        finally:
            pg.close()
    except Exception as error:  # noqa: BLE001 - ダッシュボードは失敗で止めない
        log.warning("skill growth collection failed: %s", error)
        return empty

    skill_candidates = list(proposal.get("skill_candidates", []))[:10]
    gap_candidates = list(proposal.get("gap_candidates", []))[:10]
    action_items = list(proposal.get("action_items", []))[:10]

    return {
        "summary": proposal.get("summary", empty["summary"]),
        "skill_candidates": skill_candidates,
        "gap_candidates": gap_candidates,
        "action_items": action_items,
        "chart_labels": [str(item.get("suggested_name", "")) for item in skill_candidates],
        "chart_scores": [int(item.get("priority_score", 0) or 0) for item in skill_candidates],
    }


def handle_import(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    get_git_user_name: GitUserFn,
) -> None:
    """外部データを mem に取り込む。"""
    from devgear.mem.importers import import_adrs, import_event_logs, import_instincts

    origin_user = get_git_user_name()
    types = stdin_data.get("types", ["instincts", "adrs", "events"])
    repo_root = stdin_data.get("repo_root")

    result = {"instincts": 0, "adrs": 0, "events": 0}

    with open_db(settings) as db:
        if "instincts" in types:
            result["instincts"] = import_instincts(db, origin_user)

        if "adrs" in types and repo_root:
            result["adrs"] = import_adrs(db, origin_user, repo_root)

        if "events" in types:
            result["events"] = import_event_logs(db, origin_user)

    print(json.dumps({"success": True, "imported": result}, ensure_ascii=False))


def _resolve_safe_dashboard_output_path(settings: Settings, output_value: object) -> Path | None:
    """ダッシュボードの出力先を解決し、許可ディレクトリ外のパスを拒否する。"""
    if not isinstance(output_value, str) or not output_value.strip():
        return None

    allowed_root = Path(settings.data_path).expanduser().resolve()
    candidate = Path(output_value).expanduser()
    if not candidate.is_absolute():
        candidate = allowed_root / candidate

    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None

    if not resolved.is_relative_to(allowed_root):
        return None
    return resolved


def handle_dashboard(
    settings: Settings,
    stdin_data: dict[str, Any],
    *,
    open_db: OpenDbFn,
    log: Any,
    collect_project_overview_fn: Callable[[], dict[str, object]],
    collect_skill_health_overview_fn: Callable[[dict[str, object]], dict[str, object]],
    collect_skill_growth_overview_fn: Callable[[Settings, int], dict[str, object]],
) -> None:
    """静的 HTML ダッシュボードを生成する。"""
    from devgear.mem import dashboard_queries as dq
    from devgear.mem import item_usage_queries as iq
    from devgear.mem.item_usage_queries import _PG_PLACEHOLDER, _SQLITE_PLACEHOLDER

    def _jdumps(obj: object) -> str:
        return re.sub(r"</", r"<\\/", json.dumps(obj, ensure_ascii=False))

    days = stdin_data.get("days", 30)
    output_default = str(Path(settings.data_path) / "devgear-dashboard.html")
    output_path = _resolve_safe_dashboard_output_path(settings, stdin_data.get("output", output_default))
    if output_path is None:
        print(json.dumps({"success": False, "error": "output path is not allowed"}))
        return
    output_format = stdin_data.get("format", "html")

    with open_db(settings) as db:
        sqlite_conn = db.conn
        personal_ranking = iq.item_usage_ranking(sqlite_conn, _SQLITE_PLACEHOLDER, days)
        personal_trend = iq.daily_trend(sqlite_conn, _SQLITE_PLACEHOLDER, days)
        personal_outcome = iq.outcome_distribution(sqlite_conn, _SQLITE_PLACEHOLDER, days)

    pg_available = False
    team_ranking: list = []
    team_trend: list = []
    pg_data: dict = {
        "user_activity": [],
        "project_activity": [],
        "tool_usage": [],
        "timeline": [],
        "instinct_growth": [],
        "quality": {
            "total_chunks": 0,
            "total_users": 0,
            "total_projects": 0,
            "total_sessions": 0,
            "access_rate": 0,
            "short_chunk_rate": 0,
        },
        "file_heatmap": [],
    }

    sync_cfg = settings.sync
    if sync_cfg.enabled and sync_cfg.postgres_url:
        try:
            from devgear.mem.pg_database import PgDatabase

            pg = PgDatabase(sync_cfg.postgres_url)
            if pg.test_connection():
                try:
                    pg_conn = pg._get_conn()
                    try:
                        team_ranking = iq.item_usage_ranking(pg_conn, _PG_PLACEHOLDER, days)
                        team_trend = iq.daily_trend(pg_conn, _PG_PLACEHOLDER, days)
                        pg_available = True
                    finally:
                        pg._put_conn(pg_conn)
                    pg_data = {
                        "user_activity": dq.activity_by_user(pg, days),
                        "project_activity": dq.activity_by_project(pg, days),
                        "tool_usage": dq.tool_usage_distribution(pg, days),
                        "timeline": dq.session_timeline(pg, days),
                        "instinct_growth": dq.instinct_growth(pg),
                        "quality": dq.memory_quality_metrics(pg),
                        "file_heatmap": dq.file_change_heatmap(pg, days),
                    }
                except Exception as e:
                    log.warning("既存パネルデータ取得失敗: %s", e)
                finally:
                    pg.close()
        except Exception as e:
            log.warning("PostgreSQL 接続失敗（個人データのみ表示）: %s", e)

    skill_labels, skill_personal = iq.make_ranking_data(personal_ranking, "skill")
    skill_team = iq.align_team_counts(skill_labels, team_ranking, "skill")

    cmd_labels, cmd_personal = iq.make_ranking_data(personal_ranking, "command")
    cmd_team = iq.align_team_counts(cmd_labels, team_ranking, "command")

    agent_labels, agent_personal = iq.make_ranking_data(personal_ranking, "agent")
    agent_team = iq.align_team_counts(agent_labels, team_ranking, "agent")

    personal_trend_by_date = {r["date"]: r["total"] for r in personal_trend}
    team_trend_by_date = {r["date"]: r["total"] for r in team_trend}
    all_dates = sorted(set(list(personal_trend_by_date.keys()) + list(team_trend_by_date.keys())))
    trend_personal_vals = [personal_trend_by_date.get(d, 0) for d in all_dates]
    trend_team_vals = [team_trend_by_date.get(d, 0) for d in all_dates]

    item_has_data = bool(personal_ranking)
    skill_health = collect_skill_health_overview_fn(dict(stdin_data))
    skill_growth = collect_skill_growth_overview_fn(settings, int(days))
    project_overview = collect_project_overview_fn()

    data = {
        **pg_data,
        "personal_ranking": personal_ranking,
        "team_ranking": team_ranking,
        "personal_outcome": personal_outcome,
        "skill_health": skill_health,
        "skill_growth": skill_growth,
        "project_overview": project_overview,
    }

    if output_format == "json":
        output_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"success": True, "output": str(output_path)}))
        return

    from jinja2 import Environment, FileSystemLoader, select_autoescape

    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("dashboard.html")

    html = template.render(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
        days=days,
        quality=pg_data["quality"],
        user_labels=_jdumps([d["user"] for d in pg_data["user_activity"]]),
        user_data=_jdumps([d["chunks"] for d in pg_data["user_activity"]]),
        project_labels=_jdumps([d["project"] for d in pg_data["project_activity"]]),
        project_data=_jdumps([d["chunks"] for d in pg_data["project_activity"]]),
        tool_labels=_jdumps([d["tool"] for d in pg_data["tool_usage"]]),
        tool_data=_jdumps([d["count"] for d in pg_data["tool_usage"]]),
        timeline_dates=_jdumps([d["date"] for d in pg_data["timeline"]]),
        timeline_sessions=_jdumps([d["sessions"] for d in pg_data["timeline"]]),
        timeline_chunks=_jdumps([d["chunks"] for d in pg_data["timeline"]]),
        instinct_dates=_jdumps([d["date"] for d in pg_data["instinct_growth"]]),
        instinct_counts=_jdumps([d["count"] for d in pg_data["instinct_growth"]]),
        file_heatmap=pg_data["file_heatmap"],
        pg_available=pg_available,
        item_has_data=item_has_data,
        item_skill_labels=_jdumps(skill_labels),
        item_skill_personal=_jdumps(skill_personal),
        item_skill_team=_jdumps(skill_team),
        item_command_labels=_jdumps(cmd_labels),
        item_command_personal=_jdumps(cmd_personal),
        item_command_team=_jdumps(cmd_team),
        item_agent_labels=_jdumps(agent_labels),
        item_agent_personal=_jdumps(agent_personal),
        item_agent_team=_jdumps(agent_team),
        item_trend_dates=_jdumps(all_dates),
        item_trend_personal=_jdumps(trend_personal_vals),
        item_trend_team=_jdumps(trend_team_vals),
        item_outcome_labels=_jdumps([d["outcome"] for d in personal_outcome]),
        item_outcome_personal=_jdumps([d["count"] for d in personal_outcome]),
        skill_health_summary=skill_health["summary"],
        skill_health_labels=_jdumps(skill_health["chart_labels"]),
        skill_health_7d=_jdumps(skill_health["chart_7d"]),
        skill_health_30d=_jdumps(skill_health["chart_30d"]),
        skill_health_rows=skill_health["skills"],
        skill_growth_summary=skill_growth["summary"],
        skill_growth_labels=_jdumps(skill_growth["chart_labels"]),
        skill_growth_scores=_jdumps(skill_growth["chart_scores"]),
        skill_candidates=skill_growth["skill_candidates"],
        gap_candidates=skill_growth["gap_candidates"],
        action_items=skill_growth["action_items"],
        project_summary=project_overview["summary"],
        project_rows=project_overview["projects"],
    )

    output_path.write_text(html, encoding="utf-8")
    print(json.dumps({"success": True, "output": str(output_path)}))
