"""mem CLI: dashboard/import handlers and overview collectors."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bluecore.lib.skill_evolution import collect_skill_health, summarize_health_report

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractContextManager

    from bluecore.mem.database import Database
    from bluecore.mem.settings import Settings

    OpenDbFn = Callable[[Settings], AbstractContextManager[Database]]
    GitUserFn = Callable[[], str]
    CountLinesFn = Callable[[Path], int]


@dataclass(frozen=True)
class DashboardData:
    """ダッシュボード表示に必要な全データを集約したコンテナ。"""

    days: int
    pg_data: dict
    pg_available: bool
    personal_outcome: list
    item_vars: dict
    skill_health: dict
    skill_growth: dict
    project_overview: dict


@dataclass(frozen=True)
class DashboardDeps:
    """handle_dashboard の外部依存（DB接続・コールバック・ロガー）。"""

    open_db: OpenDbFn
    log: Any
    collect_project_overview_fn: Callable[[], dict]
    collect_skill_health_overview_fn: Callable[[dict], dict]
    collect_skill_growth_overview_fn: Callable[[Settings, int], dict]


def count_lines(path: Path) -> int:
    """ファイルの行数を数える。"""
    try:
        if not path.exists():
            return 0
        with path.open(encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


def _build_project_entry(
    project_id: str,
    project_info: dict,
    *,
    load_instincts: Any,
    count_lines_fn: CountLinesFn,
    project_dir_fn: Any,
) -> dict[str, object]:
    """単一プロジェクトの集計エントリを構築する。"""
    project_dir = project_dir_fn(project_id)
    personal_count = len(load_instincts(project_dir / "instincts" / "personal", "personal", "project"))
    inherited_count = len(load_instincts(project_dir / "instincts" / "inherited", "inherited", "project"))
    return {
        "id": project_id,
        "name": project_info.get("name", project_id),
        "personal_instincts": personal_count,
        "inherited_instincts": inherited_count,
        "observations": count_lines_fn(project_dir / "observations.jsonl"),
        "last_seen": project_info.get("last_seen", "unknown"),
        "_personal": personal_count,
        "_inherited": inherited_count,
    }


def collect_project_overview(*, count_lines_fn: CountLinesFn, log: Any) -> dict:
    """既知プロジェクトと instinct の集計を返す。"""
    from bluecore.skills.learn.cli import (
        GLOBAL_INHERITED_DIR,
        GLOBAL_PERSONAL_DIR,
        _load_instincts_from_dir,
        _project_dir_for_id,
        load_registry,
    )

    registry = load_registry()
    valid_projects = [(pid, info) for pid, info in registry.items() if isinstance(info, dict)]
    if len(valid_projects) != len(registry):
        log.warning("project registry contains invalid entries; skipping them")

    projects: list[dict[str, object]] = []
    total_personal = 0
    total_inherited = 0
    for project_id, project_info in sorted(
        valid_projects, key=lambda item: str(item[1].get("last_seen", "")), reverse=True
    ):
        entry = _build_project_entry(
            project_id, project_info,
            load_instincts=_load_instincts_from_dir,
            count_lines_fn=count_lines_fn,
            project_dir_fn=_project_dir_for_id,
        )
        total_personal += entry.pop("_personal")
        total_inherited += entry.pop("_inherited")
        projects.append(entry)

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
        from bluecore.mem import skill_analyzer, skill_proposal
        from bluecore.mem.pg_database import PgDatabase

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
    from bluecore.mem.importers import import_adrs, import_event_logs, import_instincts

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


def _jdumps(obj: object) -> str:
    """HTML 埋め込み用に `</` をエスケープした JSON 文字列を返す。"""
    return re.sub(r"</", r"<\\/", json.dumps(obj, ensure_ascii=False))


_PG_DATA_EMPTY: dict = {
    "user_activity": [],
    "project_activity": [],
    "tool_usage": [],
    "timeline": [],
    "instinct_growth": [],
    "quality": {
        "total_chunks": 0, "total_users": 0, "total_projects": 0,
        "total_sessions": 0, "access_rate": 0, "short_chunk_rate": 0,
    },
    "file_heatmap": [],
}


def _fetch_pg_panel_data(pg: Any, pg_conn: Any, days: int) -> tuple[bool, list, list, dict]:
    """PG 接続が確立済みの状態でランキング・トレンド・追加パネルデータを収集する。"""
    from bluecore.mem import dashboard_queries as dq
    from bluecore.mem import item_usage_queries as iq
    from bluecore.mem.item_usage_queries import _PG_PLACEHOLDER

    team_ranking = iq.item_usage_ranking(pg_conn, _PG_PLACEHOLDER, days)
    team_trend = iq.daily_trend(pg_conn, _PG_PLACEHOLDER, days)
    pg_data = {
        "user_activity": dq.activity_by_user(pg, days),
        "project_activity": dq.activity_by_project(pg, days),
        "tool_usage": dq.tool_usage_distribution(pg, days),
        "timeline": dq.session_timeline(pg, days),
        "instinct_growth": dq.instinct_growth(pg),
        "quality": dq.memory_quality_metrics(pg),
        "file_heatmap": dq.file_change_heatmap(pg, days),
    }
    return True, team_ranking, team_trend, pg_data


def _collect_pg_dashboard_data(
    settings: Settings,
    days: int,
    *,
    log: Any,
) -> tuple[bool, list, list, dict]:
    """PostgreSQL からチームランキング・トレンド・追加パネルデータを収集する。

    Returns:
        (pg_available, team_ranking, team_trend, pg_data)
    """
    empty = (False, [], [], dict(_PG_DATA_EMPTY))
    sync_cfg = settings.sync
    if not sync_cfg.enabled or not sync_cfg.postgres_url:
        return empty

    try:
        from bluecore.mem.pg_database import PgDatabase

        pg = PgDatabase(sync_cfg.postgres_url)
        if not pg.test_connection():
            return empty
        try:
            try:
                with pg.transaction() as pg_conn:
                    pg_available, team_ranking, team_trend, pg_data = _fetch_pg_panel_data(pg, pg_conn, days)
            except Exception as e:
                log.warning("既存パネルデータ取得失敗: %s", e)
                pg_available, team_ranking, team_trend, pg_data = False, [], [], dict(_PG_DATA_EMPTY)
        finally:
            pg.close()
        return pg_available, team_ranking, team_trend, pg_data
    except Exception as e:
        log.warning("PostgreSQL 接続失敗（個人データのみ表示）: %s", e)
        return empty


def _build_item_ranking_vars(
    personal_ranking: list,
    team_ranking: list,
    personal_trend: list,
    team_trend: list,
) -> dict:
    """スキル/コマンド/エージェントのランキング・トレンド変数を構築する。"""
    from bluecore.mem import item_usage_queries as iq

    skill_labels, skill_personal = iq.make_ranking_data(personal_ranking, "skill")
    skill_team = iq.align_team_counts(skill_labels, team_ranking, "skill")

    cmd_labels, cmd_personal = iq.make_ranking_data(personal_ranking, "command")
    cmd_team = iq.align_team_counts(cmd_labels, team_ranking, "command")

    agent_labels, agent_personal = iq.make_ranking_data(personal_ranking, "agent")
    agent_team = iq.align_team_counts(agent_labels, team_ranking, "agent")

    personal_trend_by_date = {r["date"]: r["total"] for r in personal_trend}
    team_trend_by_date = {r["date"]: r["total"] for r in team_trend}
    all_dates = sorted(set(list(personal_trend_by_date.keys()) + list(team_trend_by_date.keys())))

    return {
        "skill_labels": skill_labels,
        "skill_personal": skill_personal,
        "skill_team": skill_team,
        "cmd_labels": cmd_labels,
        "cmd_personal": cmd_personal,
        "cmd_team": cmd_team,
        "agent_labels": agent_labels,
        "agent_personal": agent_personal,
        "agent_team": agent_team,
        "all_dates": all_dates,
        "trend_personal_vals": [personal_trend_by_date.get(d, 0) for d in all_dates],
        "trend_team_vals": [team_trend_by_date.get(d, 0) for d in all_dates],
    }


def _build_pg_data_ctx(pg_data: dict, pg_available: bool) -> dict:
    """PostgreSQL パネルデータをテンプレート変数辞書に変換する。"""
    return {
        "quality": pg_data["quality"],
        "user_labels": _jdumps([d["user"] for d in pg_data["user_activity"]]),
        "user_data": _jdumps([d["chunks"] for d in pg_data["user_activity"]]),
        "project_labels": _jdumps([d["project"] for d in pg_data["project_activity"]]),
        "project_data": _jdumps([d["chunks"] for d in pg_data["project_activity"]]),
        "tool_labels": _jdumps([d["tool"] for d in pg_data["tool_usage"]]),
        "tool_data": _jdumps([d["count"] for d in pg_data["tool_usage"]]),
        "timeline_dates": _jdumps([d["date"] for d in pg_data["timeline"]]),
        "timeline_sessions": _jdumps([d["sessions"] for d in pg_data["timeline"]]),
        "timeline_chunks": _jdumps([d["chunks"] for d in pg_data["timeline"]]),
        "instinct_dates": _jdumps([d["date"] for d in pg_data["instinct_growth"]]),
        "instinct_counts": _jdumps([d["count"] for d in pg_data["instinct_growth"]]),
        "file_heatmap": pg_data["file_heatmap"],
        "pg_available": pg_available,
    }


def _build_item_ctx(item_vars: dict, personal_outcome: list) -> dict:
    """スキル/コマンド/エージェントのランキング・アウトカム変数をテンプレート辞書に変換する。"""
    return {
        "item_has_data": bool(item_vars.get("skill_labels") or item_vars.get("cmd_labels")),
        "item_skill_labels": _jdumps(item_vars["skill_labels"]),
        "item_skill_personal": _jdumps(item_vars["skill_personal"]),
        "item_skill_team": _jdumps(item_vars["skill_team"]),
        "item_command_labels": _jdumps(item_vars["cmd_labels"]),
        "item_command_personal": _jdumps(item_vars["cmd_personal"]),
        "item_command_team": _jdumps(item_vars["cmd_team"]),
        "item_agent_labels": _jdumps(item_vars["agent_labels"]),
        "item_agent_personal": _jdumps(item_vars["agent_personal"]),
        "item_agent_team": _jdumps(item_vars["agent_team"]),
        "item_trend_dates": _jdumps(item_vars["all_dates"]),
        "item_trend_personal": _jdumps(item_vars["trend_personal_vals"]),
        "item_trend_team": _jdumps(item_vars["trend_team_vals"]),
        "item_outcome_labels": _jdumps([d["outcome"] for d in personal_outcome]),
        "item_outcome_personal": _jdumps([d["count"] for d in personal_outcome]),
    }


def _build_template_context(data: DashboardData) -> dict:
    """Jinja2 テンプレートに渡すコンテキスト辞書を構築する。"""
    ctx = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "days": data.days}
    ctx.update(_build_pg_data_ctx(data.pg_data, data.pg_available))
    ctx.update(_build_item_ctx(data.item_vars, data.personal_outcome))
    ctx.update({
        "skill_health_summary": data.skill_health["summary"],
        "skill_health_labels": _jdumps(data.skill_health["chart_labels"]),
        "skill_health_7d": _jdumps(data.skill_health["chart_7d"]),
        "skill_health_30d": _jdumps(data.skill_health["chart_30d"]),
        "skill_health_rows": data.skill_health["skills"],
        "skill_growth_summary": data.skill_growth["summary"],
        "skill_growth_labels": _jdumps(data.skill_growth["chart_labels"]),
        "skill_growth_scores": _jdumps(data.skill_growth["chart_scores"]),
        "skill_candidates": data.skill_growth["skill_candidates"],
        "gap_candidates": data.skill_growth["gap_candidates"],
        "action_items": data.skill_growth["action_items"],
        "project_summary": data.project_overview["summary"],
        "project_rows": data.project_overview["projects"],
    })
    return ctx


def _render_dashboard_html(output_path: Path, data: DashboardData) -> None:
    """Jinja2 テンプレートを使って HTML ダッシュボードをレンダリングして書き出す。"""
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    template_dir = Path(__file__).parent / "templates"
    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("dashboard.html")
    output_path.write_text(template.render(**_build_template_context(data)), encoding="utf-8")


def _collect_personal_stats(open_db: Any, settings: Settings, days: int) -> tuple[list, list, list]:
    """SQLite から個人のランキング・トレンド・アウトカムを収集して返す。"""
    from bluecore.mem import item_usage_queries as iq
    from bluecore.mem.item_usage_queries import _SQLITE_PLACEHOLDER

    with open_db(settings) as db:
        conn = db.conn
        return (
            iq.item_usage_ranking(conn, _SQLITE_PLACEHOLDER, days),
            iq.daily_trend(conn, _SQLITE_PLACEHOLDER, days),
            iq.outcome_distribution(conn, _SQLITE_PLACEHOLDER, days),
        )


def handle_dashboard(
    settings: Settings,
    stdin_data: dict[str, Any],
    deps: DashboardDeps,
) -> None:
    """静的 HTML ダッシュボードを生成する。"""
    try:
        days = int(stdin_data.get("days", 30))
    except (TypeError, ValueError):
        print(json.dumps({"success": False, "error": "days must be an integer"}))
        return
    output_default = str(Path(settings.data_path) / "bluecore-dashboard.html")
    output_path = _resolve_safe_dashboard_output_path(settings, stdin_data.get("output", output_default))
    if output_path is None:
        print(json.dumps({"success": False, "error": "output path is not allowed"}))
        return
    output_format = stdin_data.get("format", "html")

    personal_ranking, personal_trend, personal_outcome = _collect_personal_stats(deps.open_db, settings, days)
    pg_available, team_ranking, team_trend, pg_data = _collect_pg_dashboard_data(settings, days, log=deps.log)
    item_vars = _build_item_ranking_vars(personal_ranking, team_ranking, personal_trend, team_trend)
    skill_health = deps.collect_skill_health_overview_fn(dict(stdin_data))
    skill_growth = deps.collect_skill_growth_overview_fn(settings, days)
    project_overview = deps.collect_project_overview_fn()
    dash_data = DashboardData(
        days=days,
        pg_data=pg_data,
        pg_available=pg_available,
        personal_outcome=personal_outcome,
        item_vars=item_vars,
        skill_health=skill_health,
        skill_growth=skill_growth,
        project_overview=project_overview,
    )

    if output_format == "json":
        json_data = {
            **pg_data,
            "personal_ranking": personal_ranking,
            "team_ranking": team_ranking,
            "personal_outcome": personal_outcome,
            "skill_health": skill_health,
            "skill_growth": skill_growth,
            "project_overview": project_overview,
        }
        output_path.write_text(json.dumps(json_data, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"success": True, "output": str(output_path)}))
        return

    _render_dashboard_html(output_path, dash_data)
    print(json.dumps({"success": True, "output": str(output_path)}))
