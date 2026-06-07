"""スキル健全性を見やすいパネルに分解して表示する。

このモジュールは、実行レコードとバージョン履歴を突き合わせて、
成功率・失敗傾向・保留中の修正提案・バージョン履歴を個別の
パネルとして整形する。CLI でも読みやすいテキスト出力と、
呼び出し元が再利用しやすいデータ構造の両方を返す。
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

from . import health as health
from . import tracker as tracker
from . import versioning as versioning
from .dashboard_normalize import _collect_skill_ids, _group_records_by_skill, _iter_skill_items, _iter_skills
from .skill_evolution_compat import get_option, get_value, merge_options, parse_iso_timestamp, utc_now_iso

DAY_IN_MS = 24 * 60 * 60 * 1000
SPARKLINE_CHARS = "▁▂▃▄▅▆▇█"
EMPTY_BLOCK = "░"
FILL_BLOCK = "█"
DEFAULT_PANEL_WIDTH = 64
VALID_PANELS = {"success-rate", "failures", "amendments", "versions"}


def _round_half_up(value: float) -> int:
    """浮動小数点数を四捨五入して整数に変換する。

    Args:
        value: 丸め対象の浮動小数点数。

    Returns:
        四捨五入後の整数。

    Raises:
        なし。
    """
    return int(math.floor(value + 0.5))


def sparkline(values: list[Any] | tuple[Any, ...] | None) -> str:
    """正規化済みの値列をスパークライン文字列に変換する。

    Args:
        values: 0.0〜1.0 に正規化済みの値列。

    Returns:
        描画結果のスパークライン文字列。入力が無効な場合は空文字。

    Raises:
        なし。
    """
    # リスト/タプル以外、または空系列は描画対象外とする。
    if not isinstance(values, (list, tuple)) or len(values) == 0:
        return ""

    chars: list[str] = []
    # 各要素を順に変換し、描画できない値は空ブロックに落とす。
    for value in values:
        # 欠損値は空ブロックとして表現する。
        if value is None:
            chars.append(EMPTY_BLOCK)
            continue

        try:
            numeric = float(value)
        except (TypeError, ValueError):
            # 数値化できない要素も欠損として扱う。
            chars.append(EMPTY_BLOCK)
            continue

        # 値を 0〜1 の範囲に収め、スパークラインの段階値へ写像する。
        clamped = max(0.0, min(1.0, numeric))
        index = min(_round_half_up(clamped * (len(SPARKLINE_CHARS) - 1)), len(SPARKLINE_CHARS) - 1)
        chars.append(SPARKLINE_CHARS[index])

    return "".join(chars)


def horizontal_bar(value: float, max_value: float, width: int) -> str:
    """指定値を基準に横棒グラフ文字列を生成する。

    Args:
        value: 描画対象の値。
        max_value: バー全体の基準となる最大値。
        width: 生成するバーの文字数。

    Returns:
        横棒グラフの文字列。基準値が無効な場合は空ブロック列。

    Raises:
        なし。
    """
    # 比較基準が成立しない場合は、空のバーを返す。
    if max_value <= 0 or width <= 0:
        return EMPTY_BLOCK * max(width, 0)

    # 最大値に対する割合をバー幅へ換算し、塗りつぶし文字数を決める。
    filled = _round_half_up((min(value, max_value) / max_value) * width)
    empty = width - filled
    return FILL_BLOCK * filled + EMPTY_BLOCK * empty


def panel_box(title: str, lines: list[str], width: int | None = None) -> str:
    """罫線付きのテキストパネルを生成する。

    Args:
        title: パネルのタイトル。
        lines: パネル本文に表示する各行。
        width: パネルの内側幅。未指定時は既定幅を使う。

    Returns:
        罫線付きパネル文字列。

    Raises:
        ValueError: width を整数に変換できない場合。
    """
    # 内側幅を最低値付きで確定し、極端に狭い表示を防ぐ。
    inner_width = max(2, int(width or DEFAULT_PANEL_WIDTH))
    # タイトル分の余白を確保し、上辺の罫線が崩れないようにする。
    top_padding = max(0, inner_width - len(title) - 4)
    output = ["┌─ " + title + " " + "─" * top_padding + "┐"]

    content_width = max(0, inner_width - 2)
    # 各行を内側幅に収めてから、左右の罫線に挟んで出力する。
    for line in lines:
        # 可視幅で切り詰めて左寄せし、右端の揃いを維持する。
        truncated = line[:content_width]
        output.append("│ " + truncated.ljust(content_width) + "│")

    # 下辺は内側幅に合わせて閉じ、パネル全体を完結させる。
    output.append("└" + "─" * max(0, inner_width - 1) + "┘")
    return "\n".join(output)


def _build_day_buckets(now_ms: int, days: int) -> list[dict[str, Any]]:
    """指定日数分の空の日次バケットを生成する。

    Args:
        now_ms: 基準時刻の UNIX ミリ秒。
        days: 作成する日次バケット数。

    Returns:
        date/start/end/records を持つバケット辞書のリスト。

    Raises:
        なし。
    """
    buckets: list[dict[str, Any]] = []
    for i in range(days - 1, -1, -1):
        day_end = now_ms - (i * DAY_IN_MS)
        day_start = day_end - DAY_IN_MS
        date_str = datetime.fromtimestamp(day_end / 1000, tz=UTC).date().isoformat()
        buckets.append({"date": date_str, "start": day_start, "end": day_end, "records": []})
    return buckets


def _fill_buckets(buckets: list[dict[str, Any]], records: list[Any]) -> None:
    """レコードを対応する日次バケットへ振り分ける（インプレース変更）。

    Args:
        buckets: _build_day_buckets が生成したバケットリスト。
        records: 振り分け対象の実行レコード。

    Returns:
        なし。

    Raises:
        なし。
    """
    for record in records:
        recorded_at = get_value(record, "recorded_at", "recordedAt")
        recorded_dt = parse_iso_timestamp(recorded_at)
        # タイムスタンプが解釈できないレコードは集計対象外にする。
        if recorded_dt is None:
            continue
        record_ms = int(recorded_dt.timestamp() * 1000)
        for bucket in buckets:
            if record_ms > bucket["start"] and record_ms <= bucket["end"]:
                bucket["records"].append(record)
                break


def bucket_by_day(records: list[Any], now_ms: int, days: int) -> list[dict[str, Any]]:
    """レコードを日単位の集計バケットへ振り分ける。

    Args:
        records: 集計対象の実行レコード。
        now_ms: 基準時刻の UNIX ミリ秒。
        days: 作成する日次バケット数。

    Returns:
        各日付の成功率と実行件数を含む辞書のリスト。

    Raises:
        なし。
    """
    # 日数が不正なら集計できないため、空配列を返す。
    if days <= 0:
        return []

    buckets = _build_day_buckets(now_ms, days)
    _fill_buckets(buckets, records)

    return [
        {
            "date": bucket["date"],
            "rate": health.calculate_success_rate(bucket["records"]) if bucket["records"] else None,
            "runs": len(bucket["records"]),
        }
        for bucket in buckets
    ]


def get_trend_arrow(success_rate_7d: float | None, success_rate_30d: float | None) -> str:
    """7 日と 30 日の成功率差から傾向矢印を返す。

    Args:
        success_rate_7d: 直近 7 日間の成功率。
        success_rate_30d: 直近 30 日間の成功率。

    Returns:
        傾向矢印（↗: 改善、↘: 悪化、→: 横ばい）。

    Raises:
        なし。
    """
    # 片方でも値が欠けている場合は、傾向判定を保留する。
    if success_rate_7d is None or success_rate_30d is None:
        return "→"

    # 30 日平均との差を求め、閾値に応じて傾向を分類する。
    delta = success_rate_7d - success_rate_30d
    # 十分な改善が見られる場合は上向き矢印を返す。
    if delta >= 0.1:
        return "↗"
    # 十分な悪化が見られる場合は下向き矢印を返す。
    if delta <= -0.1:
        return "↘"
    return "→"


def format_percent(value: float | None) -> str:
    """比率を百分率表記へ整形する。

    Args:
        value: 0.0〜1.0 の比率値。

    Returns:
        百分率文字列（例: "85%"）または "n/a"。

    Raises:
        なし。
    """
    # 値が無ければ、表示上も欠損として扱う。
    if value is None:
        return "n/a"
    # パーセントへ変換してから四捨五入し、表示用の整数文字列にする。
    return f"{int(math.floor(float(value) * 100 + 0.5))}%"


def _build_skill_rate_entry(
    skill_id: str,
    skill_records: list[Any],
    now_ms: int,
    days: int,
) -> dict[str, Any]:
    """1 スキル分の成功率エントリを構築する。

    Args:
        skill_id: スキルの識別子。
        skill_records: そのスキルの実行レコードリスト。
        now_ms: 基準時刻の UNIX ミリ秒。
        days: 集計日数。

    Returns:
        skill_id/daily_rates/sparkline/current_7d/trend を持つ辞書。

    Raises:
        なし。
    """
    daily_rates = bucket_by_day(skill_records, now_ms, days)
    rate_values = [bucket["rate"] for bucket in daily_rates]
    records_7d = health.filter_records_within_days(skill_records, now_ms, 7)
    records_30d = health.filter_records_within_days(skill_records, now_ms, 30)
    current_7d = health.calculate_success_rate(records_7d)
    current_30d = health.calculate_success_rate(records_30d)
    return {
        "skill_id": skill_id,
        "daily_rates": daily_rates,
        "sparkline": sparkline(rate_values),
        "current_7d": current_7d,
        "trend": get_trend_arrow(current_7d, current_30d),
    }


def _resolve_success_rate_params(opts: dict[str, Any]) -> tuple[int, int, int]:
    """成功率パネル用パラメータを解決して返す。

    Args:
        opts: マージ済みオプション辞書。

    Returns:
        (now_ms, days, width) のタプル。

    Raises:
        ValueError: now タイムスタンプが不正な場合。
    """
    now = get_option(opts, "now", default=None) or utc_now_iso()
    now_dt = parse_iso_timestamp(now)
    if now_dt is None:
        raise ValueError(f"Invalid now timestamp: {now}")
    days = int(get_option(opts, "days", default=30))
    width = int(get_option(opts, "width", default=DEFAULT_PANEL_WIDTH))
    return int(now_dt.timestamp() * 1000), days, width


def _build_success_rate_lines(skill_data: list[dict[str, Any]]) -> list[str]:
    """スキル成功率データをパネル表示行のリストに変換する。

    Args:
        skill_data: _build_skill_rate_entry が返すデータのリスト。

    Returns:
        パネルに表示する文字列のリスト。
    """
    if not skill_data:
        return ["No skill execution data available."]
    lines: list[str] = []
    for skill in skill_data:
        name_col = str(skill["skill_id"])[:14].ljust(14)
        spark_col = skill["sparkline"][:30]
        rate_col = format_percent(skill["current_7d"]).rjust(5)
        lines.append(f"{name_col}  {spark_col}  {rate_col} {skill['trend']}")
    return lines


def render_success_rate_panel(
    records: list[Any],
    skills: Any,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """成功率パネルを描画する。

    Args:
        records: スキル実行レコードのリスト。
        skills: スキル情報（辞書またはリスト）。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        パネルテキストと集計データを含む辞書。

    Raises:
        ValueError: now タイムスタンプ、days、width のいずれかが不正な場合。
    """
    opts = merge_options(options, **kwargs)
    now_ms, days, width = _resolve_success_rate_params(opts)
    records_by_skill = _group_records_by_skill(records)
    skill_ids = _collect_skill_ids(records_by_skill, _iter_skills(skills))
    skill_data = [
        _build_skill_rate_entry(sid, records_by_skill.get(sid, []), now_ms, days)
        for sid in skill_ids
    ]
    lines = _build_success_rate_lines(skill_data)
    return {
        "text": panel_box("Success Rate (30d)", lines, width),
        "data": {"skills": skill_data},
    }


def _build_failure_clusters(failures: list[Any]) -> list[dict[str, Any]]:
    """失敗レコードを原因ごとにクラスタリングして返す。

    Args:
        failures: outcome が failure のレコードリスト。

    Returns:
        件数降順にソートされたクラスター辞書のリスト。

    Raises:
        なし。
    """
    cluster_map: dict[str, dict[str, Any]] = {}
    for record in failures:
        reason = (
            str(get_value(record, "failure_reason", "failureReason", default="unknown") or "unknown").lower().strip()
        )
        cluster = cluster_map.setdefault(reason, {"count": 0, "skill_ids": set()})
        cluster["count"] += 1
        skill_id = get_value(record, "skill_id", "skillId")
        if skill_id is not None:
            cluster["skill_ids"].add(str(skill_id))

    clusters_unsorted = [
        {
            "pattern": pattern,
            "count": data["count"],
            "skill_ids": sorted(data["skill_ids"]),
            "percentage": int(math.floor((data["count"] / len(failures)) * 100 + 0.5)) if failures else 0,
        }
        for pattern, data in cluster_map.items()
    ]
    return sorted(clusters_unsorted, key=lambda item: (-item["count"], item["pattern"]))


def render_failure_cluster_panel(
    records: list[Any],
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """失敗原因のクラスターを可視化したパネルを描画する。

    Args:
        records: スキル実行レコードのリスト。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        パネルテキストとクラスター集計を含む辞書。

    Raises:
        ValueError: width オプションを整数に変換できない場合。
    """
    opts = merge_options(options, **kwargs)
    width = int(get_option(opts, "width", default=DEFAULT_PANEL_WIDTH))
    failures = [record for record in records if get_value(record, "outcome") == "failure"]
    clusters = _build_failure_clusters(failures)

    max_count = clusters[0]["count"] if clusters else 0
    lines: list[str] = []
    if not clusters:
        lines.append("No failure patterns detected.")
    else:
        for cluster in clusters:
            label = cluster["pattern"][:20].ljust(20)
            bar = horizontal_bar(cluster["count"], max_count, 16)
            skill_count = len(cluster["skill_ids"])
            suffix = "skill" if skill_count == 1 else "skills"
            lines.append(f"{label} {bar} {str(cluster['count']).rjust(3)} ({skill_count} {suffix})")

    return {
        "text": panel_box("Failure Patterns", lines, width),
        "data": {"clusters": clusters, "total_failures": len(failures)},
    }


def _amendment_created_ms(item: dict[str, Any]) -> int:
    """修正提案の作成時刻をソート用ミリ秒に変換する。

    Args:
        item: 保留中修正提案の辞書。

    Returns:
        作成時刻のミリ秒。未指定の場合は 0。

    Raises:
        なし。
    """
    created_at = parse_iso_timestamp(item.get("created_at"))
    return int(created_at.timestamp() * 1000) if created_at is not None else 0


def _collect_pending_amendments(skills_by_id: Any) -> list[dict[str, Any]]:
    """全スキルから保留中の修正提案を収集して新しい順に返す。

    Args:
        skills_by_id: skill_id をキーにしたスキル情報。

    Returns:
        作成時刻降順にソートされた保留中修正提案の辞書リスト。

    Raises:
        なし。
    """
    amendments: list[dict[str, Any]] = []
    for skill_id, skill in _iter_skill_items(skills_by_id):
        skill_dir = skill.get("skill_dir")
        if not skill_dir:
            continue
        for entry in versioning.get_evolution_log(skill_dir, "amendments"):
            status = get_value(entry, "status")
            is_pending = (
                status in health.PENDING_AMENDMENT_STATUSES
                if isinstance(status, str)
                else get_value(entry, "event") == "proposal"
            )
            if is_pending:
                amendments.append(
                    {
                        "skill_id": skill_id,
                        "event": get_value(entry, "event", default="proposal"),
                        "status": status or "pending",
                        "created_at": get_value(entry, "created_at"),
                    }
                )
    amendments.sort(key=_amendment_created_ms, reverse=True)
    return amendments


def render_amendment_panel(
    skills_by_id: Any,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """保留中の修正提案を一覧表示するパネルを描画する。

    Args:
        skills_by_id: skill_id をキーにしたスキル情報。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        パネルテキストと保留中修正提案の一覧を含む辞書。

    Raises:
        ValueError: width オプションを整数に変換できない場合。
    """
    opts = merge_options(options, **kwargs)
    width = int(get_option(opts, "width", default=DEFAULT_PANEL_WIDTH))
    amendments = _collect_pending_amendments(skills_by_id)

    lines: list[str] = []
    if not amendments:
        lines.append("No pending amendments.")
    else:
        for amendment in amendments:
            name = str(amendment["skill_id"])[:14].ljust(14)
            event = str(amendment["event"]).ljust(10)
            status = str(amendment["status"]).ljust(10)
            time = amendment["created_at"][:19] if amendment.get("created_at") else "-"
            lines.append(f"{name} {event} {status} {time}")
        lines.append("")
        lines.append(f"{len(amendments)} amendment{'s' if len(amendments) != 1 else ''} pending review")

    return {
        "text": panel_box("Pending Amendments", lines, width),
        "data": {"amendments": amendments, "total": len(amendments)},
    }


def _build_reason_by_version(skill_dir: str) -> dict[int, str]:
    """amendments ログからバージョン番号→理由のマッピングを構築する。

    Args:
        skill_dir: スキルディレクトリのパス。

    Returns:
        バージョン番号をキーにした理由文字列の辞書。

    Raises:
        なし。
    """
    reason_by_version: dict[int, str] = {}
    for entry in versioning.get_evolution_log(skill_dir, "amendments"):
        version = get_value(entry, "version")
        reason = get_value(entry, "reason")
        if version is not None and reason is not None:
            try:
                reason_by_version[int(version)] = str(reason)
            except (TypeError, ValueError):
                continue
    return reason_by_version


def _collect_skill_versions(skills_by_id: Any) -> list[dict[str, Any]]:
    """全スキルのバージョン履歴を収集して skill_id 順に返す。

    Args:
        skills_by_id: skill_id をキーにしたスキル情報。

    Returns:
        skill_id/versions を持つ辞書のリスト（skill_id 昇順）。

    Raises:
        なし。
    """
    skill_versions: list[dict[str, Any]] = []
    for skill_id, skill in _iter_skill_items(skills_by_id):
        skill_dir = skill.get("skill_dir")
        if not skill_dir:
            continue
        versions = versioning.list_versions(skill_dir)
        if not versions:
            continue
        reason_by_version = _build_reason_by_version(skill_dir)
        version_rows = [
            {
                "version": v["version"],
                "created_at": v["created_at"],
                "reason": reason_by_version.get(int(v["version"])),
            }
            for v in versions
        ]
        skill_versions.append({"skill_id": skill_id, "versions": version_rows})
    skill_versions.sort(key=lambda item: item["skill_id"])
    return skill_versions


def render_version_timeline_panel(
    skills_by_id: Any,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """スキルのバージョン履歴タイムラインを描画する。

    Args:
        skills_by_id: skill_id をキーにしたスキル情報。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        パネルテキストとバージョン履歴を含む辞書。

    Raises:
        ValueError: width オプションを整数に変換できない場合。
    """
    opts = merge_options(options, **kwargs)
    width = int(get_option(opts, "width", default=DEFAULT_PANEL_WIDTH))
    skill_versions = _collect_skill_versions(skills_by_id)

    lines: list[str] = []
    if not skill_versions:
        lines.append("No version history available.")
    else:
        for skill in skill_versions:
            lines.append(skill["skill_id"])
            for version in skill["versions"]:
                date = version["created_at"][:10] if version.get("created_at") else "-"
                reason = version.get("reason") or "-"
                lines.append(f"  v{version['version']} ── {date} ── {reason}")

    return {
        "text": panel_box("Version History", lines, width),
        "data": {"skills": skill_versions},
    }


def _render_selected_panels(
    panel_renderers: dict[str, Any],
    selected_panel: str | None,
) -> tuple[dict[str, Any], list[str]]:
    """指定パネルまたは全パネルを描画してデータとテキスト部を返す。

    Args:
        panel_renderers: パネル名をキーにした描画関数の辞書。
        selected_panel: 単一パネル名。None の場合は全パネルを描画する。

    Returns:
        (panels データ辞書, テキスト部のリスト) のタプル。

    Raises:
        ValueError: selected_panel が不明なパネル名の場合。
    """
    if selected_panel and selected_panel not in VALID_PANELS:
        raise ValueError(f"Unknown panel: {selected_panel}. Valid panels: {', '.join(sorted(VALID_PANELS))}")

    panels: dict[str, Any] = {}
    text_parts: list[str] = []
    target_renderers = {selected_panel: panel_renderers[selected_panel]} if selected_panel else panel_renderers
    for panel_name, renderer in target_renderers.items():
        result = renderer()
        panels[panel_name] = result["data"]
        text_parts.append(result["text"])
    return panels, text_parts


def _resolve_dashboard_now(opts: dict[str, Any]) -> str:
    """ダッシュボード基準時刻を検証して返す。

    Args:
        opts: マージ済みオプション辞書。

    Returns:
        ISO タイムスタンプ文字列。

    Raises:
        ValueError: now タイムスタンプが不正な場合。
    """
    now = get_option(opts, "now", default=None) or utc_now_iso()
    if parse_iso_timestamp(now) is None:
        raise ValueError(f"Invalid now timestamp: {now}")
    return now


def _build_dashboard_header(now: str, summary: dict[str, Any]) -> str:
    """ダッシュボードのヘッダー文字列を生成する。

    Args:
        now: 生成時刻の ISO タイムスタンプ文字列。
        summary: summarize_health_report が返す要約辞書。

    Returns:
        ヘッダー文字列。
    """
    return "\n".join(
        [
            "bluecore Skill Health Dashboard",
            f"Generated: {now}",
            f"Skills: {summary['total_skills']} total, {summary['healthy_skills']} healthy, {summary['declining_skills']} declining",
            "",
        ]
    )


def render_dashboard(options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """スキル健全性ダッシュボード全体を描画する。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        ダッシュボード本文と各パネルデータを含む辞書。

    Raises:
        ValueError: now タイムスタンプ、パネル名、または各パネル描画に渡すオプションが不正な場合。
    """
    opts = merge_options(options, **kwargs)
    now = _resolve_dashboard_now(opts)
    dashboard_options = {**opts, "now": now}

    records = list(tracker.read_skill_execution_records(dashboard_options))
    skills_by_id = health.discover_skills(dashboard_options)
    report = health.collect_skill_health(dashboard_options)
    summary = health.summarize_health_report(report)

    panel_renderers = {
        "success-rate": lambda: render_success_rate_panel(records, report["skills"], dashboard_options),
        "failures": lambda: render_failure_cluster_panel(records, dashboard_options),
        "amendments": lambda: render_amendment_panel(skills_by_id, dashboard_options),
        "versions": lambda: render_version_timeline_panel(skills_by_id, dashboard_options),
    }

    selected_panel = get_option(opts, "panel", default=None)
    panels, panel_texts = _render_selected_panels(panel_renderers, selected_panel)
    header = _build_dashboard_header(now, summary)

    return {
        "text": "\n\n".join([header, *panel_texts]) + "\n",
        "data": {"generated_at": now, "summary": summary, "panels": panels},
    }


__all__ = [
    "DEFAULT_PANEL_WIDTH",
    "DAY_IN_MS",
    "EMPTY_BLOCK",
    "FILL_BLOCK",
    "SPARKLINE_CHARS",
    "VALID_PANELS",
    "bucket_by_day",
    "format_percent",
    "get_trend_arrow",
    "horizontal_bar",
    "panel_box",
    "render_amendment_panel",
    "render_dashboard",
    "render_failure_cluster_panel",
    "render_success_rate_panel",
    "render_version_timeline_panel",
    "sparkline",
]
