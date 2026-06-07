"""スキル健全性の指標を収集・集計・要約する。

このモジュールは、実行レコード・スキル定義・来歴情報を組み合わせて、
スキルごとの成功率、失敗傾向、保留中の修正提案数を算出する。
ダッシュボード表示用の整形ロジックもここでまとめる。
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from . import provenance as provenance
from . import tracker as tracker
from . import versioning as versioning
from .skill_evolution_compat import (
    get_option,
    get_value,
    merge_options,
    parse_iso_timestamp,
    utc_now_iso,
)

DAY_IN_MS = 24 * 60 * 60 * 1000
PENDING_AMENDMENT_STATUSES = {"pending", "proposed", "queued", "open"}


def _round_half_up(value: float, digits: int) -> float:
    """指定桁で四捨五入した値を返す。

    Args:
        value: 丸め対象の数値。
        digits: 小数点以下の保持桁数。

    Returns:
        四捨五入後の数値。

    Raises:
        なし。
    """
    # 小数点以下の桁数を固定し、四捨五入のブレを抑える。
    factor = 10**digits
    return math.floor(value * factor + 0.5) / factor


def round_rate(value: float | None) -> float | None:
    """比率を小数第 4 位まで JS 風の丸めで整える。

    Args:
        value: 丸める比率値。

    Returns:
        小数第 4 位まで丸めた比率、または None。

    Raises:
        なし。
    """
    # 値が無ければそのまま欠損として返す。
    if value is None:
        return None
    # 文字列などを含む入力も受けられるよう、浮動小数へ正規化する。
    return _round_half_up(float(value), 4)


def format_rate(value: float | None) -> str:
    """比率を百分率文字列へ整形する。

    Args:
        value: 0.0〜1.0 の比率値。

    Returns:
        百分率文字列（例: "85%"）または "n/a"。

    Raises:
        なし。
    """
    # 値が存在しない場合は、表示上も欠損を示す。
    if value is None:
        return "n/a"
    # 百分率へ換算してから四捨五入し、整数表記にする。
    return f"{math.floor(float(value) * 100 + 0.5)}%"


def summarize_health_report(report: dict[str, Any]) -> dict[str, int]:
    """健全性レポートを件数サマリーへ圧縮する。

    Args:
        report: collect_skill_health が返す健全性レポート辞書。

    Returns:
        total_skills, healthy_skills, declining_skills を含む要約辞書。

    Raises:
        なし。
    """
    # skills 配列の長さをそのまま総数として扱う。
    skills = report.get("skills", [])
    total_skills = len(skills)
    # declining フラグが立っているスキルだけをカウントする。
    declining_skills = len([skill for skill in skills if skill.get("declining")])
    return {
        "total_skills": total_skills,
        "healthy_skills": total_skills - declining_skills,
        "declining_skills": declining_skills,
    }


def _list_skills_in_root(root_path: str | Path | None) -> list[dict[str, Any]]:
    """指定ルート配下のスキルを列挙する。

    Args:
        root_path: スキルのルートディレクトリ。

    Returns:
        skill_id と skill_dir を持つスキル辞書のリスト。

    Raises:
        なし。
    """
    # root_path を Path に正規化し、存在しない場合は空の結果を返す。
    root = Path(str(root_path)).expanduser()
    # ルートが存在しないなら、配下のスキルも存在しない。
    if not root.exists():
        return []

    skills: list[dict[str, Any]] = []
    # ディレクトリだけを順に見て、SKILL.md を持つものを拾う。
    for entry in sorted(root.iterdir(), key=lambda path: path.name):
        # ディレクトリ単位で走査し、SKILL.md を持つものだけを採用する。
        if not entry.is_dir():
            continue
        skill_file = entry / "SKILL.md"
        # SKILL.md があるディレクトリのみをスキルとして採用する。
        if skill_file.exists():
            skills.append({"skill_id": entry.name, "skill_dir": str(entry)})
    return skills


def discover_skills(options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, dict[str, Any]]:
    """各スキルルートからスキル定義を検出して辞書化する。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        skill_id をキーにしたスキル辞書。

    Raises:
        なし。
    """
    opts = merge_options(options, **kwargs)
    # provenance 側の既定ルートを基準に、必要なら上書きされたパスを採用する。
    roots = provenance.get_skill_roots(opts)

    curated_root = get_option(opts, "skills_root", "skillsRoot", default=roots["curated"])
    learned_root = get_option(opts, "learned_root", "learnedRoot", default=roots["learned"])
    imported_root = get_option(opts, "imported_root", "importedRoot", default=roots["imported"])

    discovered: list[dict[str, Any]] = []
    # 各ルートのスキルに provenance の種別を付けて統合する。
    for skill in _list_skills_in_root(curated_root):
        discovered.append({**skill, "skill_type": provenance.SKILL_TYPES["CURATED"]})
    # learned ルートのスキルも同じ形式で追加する。
    for skill in _list_skills_in_root(learned_root):
        discovered.append({**skill, "skill_type": provenance.SKILL_TYPES["LEARNED"]})
    # imported ルートのスキルも同じ形式で追加する。
    for skill in _list_skills_in_root(imported_root):
        discovered.append({**skill, "skill_type": provenance.SKILL_TYPES["IMPORTED"]})

    skills_by_id: dict[str, dict[str, Any]] = {}
    # 各ルートから集めたスキルを、skill_id をキーにして重複排除する。
    for skill in discovered:
        skill_id = skill["skill_id"]
        # 同一 skill_id は最初に見つかった定義を優先する。
        if skill_id not in skills_by_id:
            skills_by_id[skill_id] = skill

    return skills_by_id


def calculate_success_rate(records: list[Any] | tuple[Any, ...] | set[Any]) -> float | None:
    """レコード集合の成功率を計算する。

    Args:
        records: 実行レコードのコレクション。

    Returns:
        成功率。レコードが空なら None。

    Raises:
        なし。
    """
    record_list = list(records)
    # レコードが 0 件なら成功率を定義できない。
    if not record_list:
        return None

    successful_records = 0
    # レコードを順に確認し、成功件数だけを数える。
    for record in record_list:
        # outcome が success のものだけを数え、成功率を算出する。
        if get_value(record, "outcome") == "success":
            successful_records += 1

    return round_rate(successful_records / len(record_list))


def filter_records_within_days(records: list[Any], now_ms: int, days: int) -> list[Any]:
    """指定日数の時間ウィンドウ内にあるレコードを抽出する。

    Args:
        records: 実行レコードのリスト。
        now_ms: 基準時刻の UNIX ミリ秒。
        days: 遡る日数。

    Returns:
        条件を満たすレコードのリスト。

    Raises:
        なし。
    """
    # 過去側の境界を計算し、その範囲内だけを残す。
    cutoff = now_ms - (days * DAY_IN_MS)
    filtered: list[Any] = []
    # 各レコードの recorded_at を確認し、時間窓内のものだけ残す。
    for record in records:
        recorded_at = get_value(record, "recorded_at", "recordedAt")
        recorded_at_dt = parse_iso_timestamp(recorded_at)
        # 解析できない日時は集計から除外する。
        if recorded_at_dt is None:
            continue

        # レコード時刻をミリ秒化して、ウィンドウ内かどうかを判定する。
        recorded_ms = int(recorded_at_dt.timestamp() * 1000)
        # 現在時刻を超えない範囲だけを採用する。
        if recorded_ms > cutoff and recorded_ms <= now_ms:
            filtered.append(record)

    return filtered


def get_failure_trend(
    success_rate_7d: float | None,
    success_rate_30d: float | None,
    warn_threshold: float,
) -> str:
    """7 日成功率と 30 日成功率から傾向を判定する。

    Args:
        success_rate_7d: 直近 7 日間の成功率。
        success_rate_30d: 直近 30 日間の成功率。
        warn_threshold: 悪化・改善とみなす閾値。

    Returns:
        worsening、improving、stable のいずれか。

    Raises:
        なし。
    """
    # どちらか欠けていれば比較不能なので stable とする。
    if success_rate_7d is None or success_rate_30d is None:
        return "stable"

    # 直近 7 日と 30 日を比較し、しきい値を超えた変化だけを傾向として返す。
    delta = round_rate(success_rate_7d - success_rate_30d)
    # 差分が計算できない場合は stable を返す（防御的分岐）。
    if delta is None:
        return "stable"
    # 閾値以上の低下は悪化として扱う。
    if delta <= (-1 * warn_threshold):
        return "worsening"
    # 閾値以上の改善は改善として扱う。
    if delta >= warn_threshold:
        return "improving"
    return "stable"


def count_pending_amendments(skill_dir: str | Path | None) -> int:
    """スキルの保留中修正提案数を数える。

    Args:
        skill_dir: スキルディレクトリ。未指定なら 0 を返す。

    Returns:
        保留中の修正提案数。

    Raises:
        なし。
    """
    # スキルディレクトリが無ければ、保留件数は数えられない。
    if not skill_dir:
        return 0

    pending = 0
    # amendments ログを順に見て、保留状態のレコードだけ数える。
    for entry in versioning.get_evolution_log(skill_dir, "amendments"):
        status = get_value(entry, "status")
        event = get_value(entry, "event")
        # status が付いていればそれを優先し、なければ proposal を保留扱いにする。
        # まず status の有無を確認する。
        if isinstance(status, str):
            # 既知の保留ステータスのみをカウントする。
            if status in PENDING_AMENDMENT_STATUSES:
                pending += 1
        # status が無い提案は、proposal イベントだけを保留扱いにする。
        elif event == "proposal":
            pending += 1

    return pending


def get_last_run(records: list[Any]) -> str | None:
    """レコード一覧から最新の recorded_at を取得する。

    Args:
        records: 実行レコードのリスト。

    Returns:
        最新の recorded_at 文字列。無効または空なら None。

    Raises:
        なし。
    """
    # レコードが無ければ最新値も存在しない。
    if not records:
        return None

    latest_timestamp: str | None = None
    latest_ms: int | None = None
    # すべてのレコードを走査して、最も新しい recorded_at を探す。
    for record in records:
        timestamp = get_value(record, "recorded_at", "recordedAt")
        recorded_dt = parse_iso_timestamp(timestamp)
        # 日時が解釈できないレコードは比較対象にしない。
        if recorded_dt is None:
            continue

        recorded_ms = int(recorded_dt.timestamp() * 1000)
        # その時点で最も新しい記録だけを保持する。
        if latest_ms is None or recorded_ms > latest_ms:
            latest_ms = recorded_ms
            latest_timestamp = timestamp

    return latest_timestamp


def _resolve_now_ms(now_value: Any) -> int:
    """now 値を UNIX ミリ秒へ変換する。

    Args:
        now_value: ISO タイムスタンプ文字列。

    Returns:
        ミリ秒 UNIX タイムスタンプ。

    Raises:
        ValueError: now タイムスタンプが不正な場合。
    """
    now_dt = parse_iso_timestamp(now_value)
    # now が解釈できない場合は、基準時刻にできない。
    if now_dt is None:
        raise ValueError(f"Invalid now timestamp: {now_value}")
    return int(now_dt.timestamp() * 1000)


def _record_skill_id(record: Any) -> str | None:
    """レコードから skill_id を抽出する。

    Args:
        record: スキル実行レコード。

    Returns:
        スキル ID、または抽出できない場合は None。

    Raises:
        なし。
    """
    value = get_value(record, "skill_id", "skillId")
    # 空文字列や空白のみの値は、実質的に未設定として扱う。
    if isinstance(value, str) and value.strip() != "":
        return value
    return None


def _resolve_warn_threshold(opts: dict[str, Any]) -> float:
    """オプションから警告閾値を解決して検証する。

    Args:
        opts: マージ済みオプション辞書。

    Returns:
        正の浮動小数点閾値。

    Raises:
        ValueError: warn_threshold が不正な場合。
    """
    warn_threshold_value = get_option(opts, "warn_threshold", "warnThreshold", default=0.1)
    try:
        warn_threshold = float(warn_threshold_value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid warn threshold: {warn_threshold_value}") from error
    # 閾値は負値を許さず、比較用のしきい値として扱う。
    if warn_threshold < 0:
        raise ValueError(f"Invalid warn threshold: {warn_threshold_value}")
    return warn_threshold


def _group_records_by_skill_id(records: list[Any]) -> dict[str, list[Any]]:
    """実行レコードを skill_id でグループ化する。

    Args:
        records: 実行レコードのリスト。

    Returns:
        skill_id をキーにしたレコードリストの辞書。

    Raises:
        なし。
    """
    records_by_skill: dict[str, list[Any]] = {}
    for record in records:
        skill_id = _record_skill_id(record)
        # skill_id が無いものはスキル別集計へ載せない。
        if skill_id is None:
            continue
        records_by_skill.setdefault(skill_id, []).append(record)
    return records_by_skill


def _compute_skill_metrics(
    skill_id: str,
    skill: dict[str, Any],
    skill_records: list[Any],
    now_ms: int,
    warn_threshold: float,
) -> dict[str, Any]:
    """1 スキル分の健全性メトリクスを計算する。

    Args:
        skill_id: スキルの識別子。
        skill: スキル定義辞書。
        skill_records: そのスキルの実行レコードリスト。
        now_ms: 基準時刻の UNIX ミリ秒。
        warn_threshold: 悪化判定に使う閾値。

    Returns:
        スキルの健全性メトリクス辞書。

    Raises:
        なし。
    """
    records_7d = filter_records_within_days(skill_records, now_ms, 7)
    records_30d = filter_records_within_days(skill_records, now_ms, 30)
    success_rate_7d = calculate_success_rate(records_7d)
    success_rate_30d = calculate_success_rate(records_30d)
    skill_dir = skill.get("skill_dir")
    # ディレクトリがある場合のみ、現在のバージョン番号を参照する。
    current_version = versioning.get_current_version(skill_dir) if skill_dir else 0
    failure_trend = get_failure_trend(success_rate_7d, success_rate_30d, warn_threshold)

    return {
        "skill_id": skill_id,
        "skill_type": skill.get("skill_type", provenance.SKILL_TYPES["UNKNOWN"]),
        "current_version": f"v{current_version}" if current_version > 0 else None,
        "pending_amendments": count_pending_amendments(skill_dir),
        "success_rate_7d": success_rate_7d,
        "success_rate_30d": success_rate_30d,
        "failure_trend": failure_trend,
        "declining": failure_trend == "worsening",
        "last_run": get_last_run(skill_records),
        "run_count_7d": len(records_7d),
        "run_count_30d": len(records_30d),
    }


def collect_skill_health(options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """スキル健全性メトリクスを収集する。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        収集時刻、閾値、スキル別メトリクスを含む辞書。

    Raises:
        ValueError: now または warn_threshold が不正な場合。
    """
    opts = merge_options(options, **kwargs)
    # 評価基準時刻を決め、後続の全計算をこの時刻に揃える。
    now = get_option(opts, "now", default=None) or utc_now_iso()
    now_ms = _resolve_now_ms(now)
    warn_threshold = _resolve_warn_threshold(opts)

    # 実行レコードとスキル一覧をそれぞれ収集し、同じ基準時刻で評価する。
    records = list(tracker.read_skill_execution_records(opts))
    skills_by_id = discover_skills(opts)
    records_by_skill = _group_records_by_skill_id(records)

    # 実行履歴しかない skill_id も、未知スキルとして後から拾えるようにする。
    for skill_id in records_by_skill:
        if skill_id not in skills_by_id:
            skills_by_id[skill_id] = {
                "skill_id": skill_id,
                "skill_dir": None,
                "skill_type": provenance.SKILL_TYPES["UNKNOWN"],
            }

    skills = [
        _compute_skill_metrics(
            skill_id,
            skills_by_id[skill_id],
            records_by_skill.get(skill_id, []),
            now_ms,
            warn_threshold,
        )
        for skill_id in sorted(skills_by_id)
    ]

    return {
        "generated_at": now,
        "warn_threshold": warn_threshold,
        "skills": skills,
    }


def _format_skill_row(skill: dict[str, Any]) -> str:
    """スキル 1 件分の表示行を整形する。

    Args:
        skill: _compute_skill_metrics が返すスキルメトリクス辞書。

    Returns:
        表形式の整形済み文字列。

    Raises:
        なし。
    """
    status_label = "!" if skill.get("declining") else " "
    return " ".join(
        [
            f"{status_label}{str(skill.get('skill_id', ''))[:14]}".ljust(16),
            str(skill.get("current_version") or "-").ljust(9),
            format_rate(skill.get("success_rate_7d")).ljust(6),
            format_rate(skill.get("success_rate_30d")).ljust(6),
            str(skill.get("failure_trend") or "stable").ljust(11),
            str(skill.get("pending_amendments", 0)).ljust(9),
            str(skill.get("last_run") or "-"),
        ]
    )


def format_health_report(report: dict[str, Any], options: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """健全性レポートをテキストまたは JSON に整形する。

    Args:
        report: collect_skill_health が返す健全性レポート。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        フォーマット済み文字列。json=True のときは JSON、そうでなければテキスト。

    Raises:
        なし。
    """
    opts = merge_options(options, **kwargs)
    json_mode = bool(get_option(opts, "json", default=False))
    # JSON モードでは、整形を加えずそのまま出力する。
    if json_mode:
        return f"{json.dumps(report, indent=2)}\n"

    summary = summarize_health_report(report)
    skills = report.get("skills", [])

    # スキルが無い場合は、空状態メッセージを返す。
    if not skills:
        return "\n".join(
            [
                "bluecore skill health",
                f"Generated: {report.get('generated_at')}",
                "",
                "No skill execution records found.",
                "",
            ]
        )

    lines = [
        "bluecore skill health",
        f"Generated: {report.get('generated_at')}",
        f"Skills: {summary['total_skills']} total, {summary['healthy_skills']} healthy, {summary['declining_skills']} declining",
        "",
        "skill            version   7d     30d    trend       pending   last run",
        "--------------------------------------------------------------------------",
    ]
    # 各スキルを 1 行ずつ整形して、読みやすい一覧にする。
    lines.extend(_format_skill_row(skill) for skill in skills)
    return "\n".join(lines) + "\n"


__all__ = [
    "DAY_IN_MS",
    "PENDING_AMENDMENT_STATUSES",
    "calculate_success_rate",
    "collect_skill_health",
    "count_pending_amendments",
    "discover_skills",
    "filter_records_within_days",
    "format_health_report",
    "format_rate",
    "get_failure_trend",
    "get_last_run",
    "round_rate",
    "summarize_health_report",
]
