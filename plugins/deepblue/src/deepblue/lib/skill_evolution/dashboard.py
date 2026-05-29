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

    buckets: list[dict[str, Any]] = []
    # 古い日付から新しい日付へ向かって 24 時間単位のバケットを作る。
    for i in range(days - 1, -1, -1):
        # 古い日付から順に 24 時間バケットを作り、時系列表示を安定させる。
        day_end = now_ms - (i * DAY_IN_MS)
        day_start = day_end - DAY_IN_MS
        date_str = datetime.fromtimestamp(day_end / 1000, tz=UTC).date().isoformat()
        buckets.append({"date": date_str, "start": day_start, "end": day_end, "records": []})

    # 各レコードを、該当する日次バケットへ一度だけ振り分ける。
    for record in records:
        recorded_at = get_value(record, "recorded_at", "recordedAt")
        recorded_dt = parse_iso_timestamp(recorded_at)
        # タイムスタンプが解釈できないレコードは集計対象外にする。
        if recorded_dt is None:
            continue

        # レコード時刻をミリ秒へ変換し、どのバケットに入るかを判定する。
        record_ms = int(recorded_dt.timestamp() * 1000)
        # 各バケットへレコードを振り分ける。
        for bucket in buckets:
            # バケット境界内に入ったレコードだけを追加する。
            if record_ms > bucket["start"] and record_ms <= bucket["end"]:
                bucket["records"].append(record)
                break

    # バケットを表示用の要約辞書へ変換する。
    summaries: list[dict[str, Any]] = []
    # 各バケットを要約へ変換する。
    for bucket in buckets:
        summaries.append(
            {
                "date": bucket["date"],
                "rate": health.calculate_success_rate(bucket["records"]) if bucket["records"] else None,
                "runs": len(bucket["records"]),
            }
        )
    return summaries


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


def _iter_skills(skills: Any) -> list[dict[str, Any]]:
    """スキル集合を辞書リストへ正規化する。

    Args:
        skills: スキル集合。辞書またはリストを想定する。

    Returns:
        スキル辞書のリスト。

    Raises:
        なし。
    """
    # 未指定なら空のスキル集合として扱う。
    if skills is None:
        return []
    # 辞書形式なら values() を返し、順不同な単純リストに揃える。
    if isinstance(skills, dict):
        return list(skills.values())
    return list(skills)


def _iter_skill_items(skills_by_id: Any) -> list[tuple[str, dict[str, Any]]]:
    """スキル集合を (skill_id, skill_data) のタプル列へ正規化する。

    Args:
        skills_by_id: スキル辞書またはリスト。

    Returns:
        (スキル ID, スキルデータ) のタプルリスト。

    Raises:
        なし。
    """
    # 未指定なら空の列として返す。
    if skills_by_id is None:
        return []
    # 辞書形式なら items() をそのまま使う。
    if isinstance(skills_by_id, dict):
        return list(skills_by_id.items())

    # リスト形式では各要素から skill_id を取り出してタプル化する。
    items: list[tuple[str, dict[str, Any]]] = []
    # 各要素を順にタプル化する。
    for skill in skills_by_id:
        skill_id = get_value(skill, "skill_id", "skillId")
        # ID が無い要素は表示対象にできないため除外する。
        if skill_id is None:
            continue
        items.append((str(skill_id), skill))
    return items


def _group_records_by_skill(records: list[Any]) -> dict[str, list[Any]]:
    """実行レコードを skill_id ごとにグループ化する。

    Args:
        records: スキル実行レコードのリスト。

    Returns:
        skill_id をキーとするレコードリストの辞書。

    Raises:
        なし。
    """
    grouped: dict[str, list[Any]] = {}
    # すべてのレコードを skill_id ごとに束ねる。
    for record in records:
        skill_id = get_value(record, "skill_id", "skillId")
        # skill_id が無いレコードは、どのスキルにも紐づけられない。
        if skill_id is None:
            continue
        # setdefault で対象スキルの配列を初期化し、そのまま追加する。
        grouped.setdefault(str(skill_id), []).append(record)
    return grouped


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
    # 集計基準時刻を確定し、日次/週次/月次の計算を同じ瞬間で揃える。
    now = get_option(opts, "now", default=None) or utc_now_iso()
    now_dt = parse_iso_timestamp(now)
    # now が解釈できない場合は、集計を続けられない。
    if now_dt is None:
        raise ValueError(f"Invalid now timestamp: {now}")

    # 表示幅と集計日数をオプションから解決する。
    days = int(get_option(opts, "days", default=30))
    width = int(get_option(opts, "width", default=DEFAULT_PANEL_WIDTH))
    now_ms = int(now_dt.timestamp() * 1000)
    skill_list = _iter_skills(skills)
    records_by_skill = _group_records_by_skill(records)

    # レコード側と定義側の skill_id を突き合わせ、表示対象を漏れなく集める。
    skill_data: list[dict[str, Any]] = []
    defined_skill_ids: set[str] = set()
    # スキル定義側の skill_id も個別に収集する。
    for skill in skill_list:
        skill_id = skill.get("skill_id")
        # skill_id があるものだけを表示対象へ加える。
        if skill_id is not None:
            defined_skill_ids.add(str(skill_id))
    skill_ids = sorted({*records_by_skill.keys(), *defined_skill_ids})

    # 各スキルについて、日次推移と 7 日/30 日の成功率を算出する。
    for skill_id in skill_ids:
        skill_records = records_by_skill.get(skill_id, [])
        daily_rates = bucket_by_day(skill_records, now_ms, days)
        rate_values = [bucket["rate"] for bucket in daily_rates]
        records_7d = health.filter_records_within_days(skill_records, now_ms, 7)
        records_30d = health.filter_records_within_days(skill_records, now_ms, 30)
        current_7d = health.calculate_success_rate(records_7d)
        current_30d = health.calculate_success_rate(records_30d)
        skill_data.append(
            {
                "skill_id": skill_id,
                "daily_rates": daily_rates,
                "sparkline": sparkline(rate_values),
                "current_7d": current_7d,
                "trend": get_trend_arrow(current_7d, current_30d),
            }
        )

    # パネル本文の各行を組み立てる。
    lines: list[str] = []
    # データが無い場合は空状態メッセージを表示する。
    if not skill_data:
        lines.append("No skill execution data available.")
    # データがある場合は、各スキルを 1 行ずつ整形する。
    else:
        # 各スキルを 1 行ずつ整形して、一覧へ追加する。
        for skill in skill_data:
            name_col = str(skill["skill_id"])[:14].ljust(14)
            spark_col = skill["sparkline"][:30]
            rate_col = format_percent(skill["current_7d"]).rjust(5)
            lines.append(f"{name_col}  {spark_col}  {rate_col} {skill['trend']}")

    return {
        "text": panel_box("Success Rate (30d)", lines, width),
        "data": {"skills": skill_data},
    }


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
    # failure_reason ごとに集約して、失敗の偏りを見える化する。
    failures = [record for record in records if get_value(record, "outcome") == "failure"]

    cluster_map: dict[str, dict[str, Any]] = {}
    # 失敗レコードを原因ごとに束ね、件数と影響範囲を集める。
    for record in failures:
        reason = (
            str(get_value(record, "failure_reason", "failureReason", default="unknown") or "unknown").lower().strip()
        )
        cluster = cluster_map.setdefault(reason, {"count": 0, "skill_ids": set()})
        # 件数と関連 skill_id を同時に蓄積する。
        cluster["count"] += 1
        skill_id = get_value(record, "skill_id", "skillId")
        # skill_id がある失敗だけ、影響範囲の表示に含める。
        if skill_id is not None:
            cluster["skill_ids"].add(str(skill_id))

    # 件数の多い順に並べ、同数なら原因文字列で安定ソートする。
    clusters_unsorted: list[dict[str, Any]] = []
    # 各原因を表示用の辞書へ整形する。
    for pattern, data in cluster_map.items():
        clusters_unsorted.append(
            {
                "pattern": pattern,
                "count": data["count"],
                "skill_ids": sorted(data["skill_ids"]),
                "percentage": int(math.floor((data["count"] / len(failures)) * 100 + 0.5)) if failures else 0,
            }
        )
    clusters = sorted(clusters_unsorted, key=lambda item: (-item["count"], item["pattern"]))

    max_count = clusters[0]["count"] if clusters else 0
    lines: list[str] = []
    # 失敗が無い場合は空状態メッセージを表示する。
    if not clusters:
        lines.append("No failure patterns detected.")
    # クラスターがある場合は、バー付きで一覧化する。
    else:
        # 各クラスターをバーと件数付きで 1 行ずつ表示する。
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
    amendments: list[dict[str, Any]] = []

    # 各スキルの進化ログから、未適用の修正提案だけを拾い上げる。
    for skill_id, skill in _iter_skill_items(skills_by_id):
        skill_dir = skill.get("skill_dir")
        # スキルディレクトリが無いものは履歴を辿れないため除外する。
        if not skill_dir:
            continue

        # amendments ログから保留候補を抽出する。
        for entry in versioning.get_evolution_log(skill_dir, "amendments"):
            status = get_value(entry, "status")
            # status があればそれを優先し、無い場合は proposal を保留扱いにする。
            is_pending = (
                status in health.PENDING_AMENDMENT_STATUSES
                if isinstance(status, str)
                else get_value(entry, "event") == "proposal"
            )
            # 保留状態のものだけを一覧へ積む。
            if is_pending:
                amendments.append(
                    {
                        "skill_id": skill_id,
                        "event": get_value(entry, "event", default="proposal"),
                        "status": status or "pending",
                        "created_at": get_value(entry, "created_at"),
                    }
                )

    def _created_ms(item: dict[str, Any]) -> int:
        """修正提案の作成時刻をソート用ミリ秒に変換する。

        Args:
            item: 保留中修正提案の辞書。

        Returns:
            作成時刻のミリ秒。未指定の場合は 0。

        Raises:
            例外は発生しません。
        """
        # created_at が無い項目は末尾に送る。
        created_at = parse_iso_timestamp(item.get("created_at"))
        return int(created_at.timestamp() * 1000) if created_at is not None else 0

    # 新しい提案を先頭に並べるため、作成時刻の降順で並べ替える。
    amendments.sort(key=_created_ms, reverse=True)

    lines: list[str] = []
    # 保留提案が無い場合は空状態メッセージを表示する。
    if not amendments:
        lines.append("No pending amendments.")
    # 保留提案がある場合は、1 件ずつ表形式で表示する。
    else:
        # それぞれの提案を、スキル ID・種別・状態・時刻の順で整形する。
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
    skill_versions: list[dict[str, Any]] = []

    # 各スキルのバージョン一覧と、それに紐づく理由を収集する。
    for skill_id, skill in _iter_skill_items(skills_by_id):
        skill_dir = skill.get("skill_dir")
        # スキルディレクトリが無い場合は履歴を参照できない。
        if not skill_dir:
            continue

        versions = versioning.list_versions(skill_dir)
        # バージョンが無いスキルはタイムラインを描画しない。
        if not versions:
            continue

        reason_by_version: dict[int, str] = {}
        # amendments ログを走査して、各バージョンの理由を引く辞書を作る。
        for entry in versioning.get_evolution_log(skill_dir, "amendments"):
            version = get_value(entry, "version")
            reason = get_value(entry, "reason")
            # version と reason が揃っている場合だけ対応付ける。
            if version is not None and reason is not None:
                try:
                    reason_by_version[int(version)] = str(reason)
                except (TypeError, ValueError):
                    continue

        # 表示用の version 一覧へ整形し、理由情報も添える。
        version_rows: list[dict[str, Any]] = []
        # 各スナップショットを、表示に必要な最小情報へ変換する。
        for version in versions:
            version_rows.append(
                {
                    "version": version["version"],
                    "created_at": version["created_at"],
                    "reason": reason_by_version.get(int(version["version"])),
                }
            )

        skill_versions.append(
            {
                "skill_id": skill_id,
                "versions": version_rows,
            }
        )

    # skill_id 順に並べて、表示の安定性を確保する。
    skill_versions.sort(key=lambda item: item["skill_id"])

    lines: list[str] = []
    # 履歴が無い場合は空状態メッセージを表示する。
    if not skill_versions:
        lines.append("No version history available.")
    # 履歴がある場合は、スキルごとにバージョンを列挙する。
    else:
        # まずスキル ID ごとの見出しを出す。
        for skill in skill_versions:
            lines.append(skill["skill_id"])
            # 各バージョンを日付と理由付きで 1 行ずつ出力する。
            for version in skill["versions"]:
                date = version["created_at"][:10] if version.get("created_at") else "-"
                reason = version.get("reason") or "-"
                lines.append(f"  v{version['version']} ── {date} ── {reason}")

    return {
        "text": panel_box("Version History", lines, width),
        "data": {"skills": skill_versions},
    }


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
    # ダッシュボード全体の基準時刻を決め、後続処理を同一時刻で揃える。
    now = get_option(opts, "now", default=None) or utc_now_iso()
    now_dt = parse_iso_timestamp(now)
    # now が不正なら全体の集計を止める。
    if now_dt is None:
        raise ValueError(f"Invalid now timestamp: {now}")

    # パネル生成に使うオプションへ now を埋め込み、各集計で再利用する。
    dashboard_options = dict(opts)
    dashboard_options["now"] = now

    # レコード・スキル定義・健全性指標を同じ基準時刻で揃えて集計する。
    records = list(tracker.read_skill_execution_records(dashboard_options))
    skills_by_id = health.discover_skills(dashboard_options)
    report = health.collect_skill_health(dashboard_options)
    summary = health.summarize_health_report(report)

    # 各パネルの描画処理を名前付きでまとめる。
    panel_renderers = {
        "success-rate": lambda: render_success_rate_panel(records, report["skills"], dashboard_options),
        "failures": lambda: render_failure_cluster_panel(records, dashboard_options),
        "amendments": lambda: render_amendment_panel(skills_by_id, dashboard_options),
        "versions": lambda: render_version_timeline_panel(skills_by_id, dashboard_options),
    }

    selected_panel = get_option(opts, "panel", default=None)
    # 個別パネル指定がある場合は、許可済みパネルだけを受け入れる。
    if selected_panel and selected_panel not in VALID_PANELS:
        raise ValueError(f"Unknown panel: {selected_panel}. Valid panels: {', '.join(sorted(VALID_PANELS))}")

    panels: dict[str, Any] = {}
    # 最初にヘッダーを出し、その後に選択パネルまたは全パネルを連結する。
    text_parts = [
        "\n".join(
            [
                "devgear Skill Health Dashboard",
                f"Generated: {now}",
                f"Skills: {summary['total_skills']} total, {summary['healthy_skills']} healthy, {summary['declining_skills']} declining",
                "",
            ]
        )
    ]

    # 単一パネル指定ならそのパネルだけを描画する。
    if selected_panel:
        result = panel_renderers[selected_panel]()
        panels[selected_panel] = result["data"]
        text_parts.append(result["text"])
    # パネル指定が無い場合は、全パネルをまとめて描画する。
    else:
        # 指定が無い場合は、全パネルを順番に描画する。
        for panel_name, renderer in panel_renderers.items():
            result = renderer()
            panels[panel_name] = result["data"]
            text_parts.append(result["text"])

    return {
        "text": "\n\n".join(text_parts) + "\n",
        "data": {
            "generated_at": now,
            "summary": summary,
            "panels": panels,
        },
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
