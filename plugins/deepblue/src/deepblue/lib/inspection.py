"""
スキル実行の失敗パターンを集計し、再発傾向を検出します。
失敗理由の正規化、パターン検出、修復アクションの提案をまとめて扱います。
状態ストアから検査レポートを生成する入口もここにあります。
"""

from __future__ import annotations

import re
from typing import Any

from .skill_evolution.skill_evolution_compat import get_option, get_value, merge_options, utc_now_iso

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_WINDOW_SIZE = 50
FAILURE_OUTCOMES = {"failure", "failed", "error"}


def normalize_failure_reason(reason: Any) -> str:
    """生の失敗理由をグループ化用に正規化する。

    Args:
        reason: 失敗理由

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    if not reason or not isinstance(reason, str):
        return "unknown"

    normalized = reason.strip().lower()
    normalized = re.sub(
        r"\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}[.\dz]*",
        "<timestamp>",
        normalized,
    )
    normalized = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<uuid>",
        normalized,
    )
    normalized = re.sub(r"/[\w./-]+", "<path>", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized or "unknown"


def _record_value(record: Any, *names: str, default: Any = None) -> Any:
    """レコードから指定名の値を取得する。

    Args:
        record: レコード
        default: default の値
        names: names の値

    Returns:
        Any: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    return get_value(record, *names, default=default)


def group_failures(skill_runs: list[Any] | tuple[Any, ...]) -> dict[str, dict[str, Any]]:
    """スキル実行をスキルと正規化した失敗理由ごとにグループ化する。

    Args:
        skill_runs: skill_runs の値

    Returns:
        dict[str, dict[str, Any]]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    groups: dict[str, dict[str, Any]] = {}

    for run in skill_runs:
        outcome = str(_record_value(run, "outcome", default="")).lower()
        if outcome not in FAILURE_OUTCOMES:
            continue

        skill_id = _record_value(run, "skillId", "skill_id", default="unknown")
        normalized_reason = normalize_failure_reason(_record_value(run, "failureReason", "failure_reason"))
        key = f"{skill_id}::{normalized_reason}"

        group = groups.setdefault(
            key,
            {
                "skillId": skill_id,
                "normalizedReason": normalized_reason,
                "runs": [],
            },
        )
        group["runs"].append(run)

    return groups


def _unique_in_order(values: list[Any]) -> list[Any]:
    """順序を保ったまま重複を除去する。

    Args:
        values: values の値

    Returns:
        list[Any]: Any の一覧を返します。

    Raises:
        例外は発生しません。
    """
    seen: set[Any] = set()
    result: list[Any] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _build_pattern_entry(group: dict[str, Any], sorted_runs: list[Any]) -> dict[str, Any]:
    """ソート済み実行リストからパターンエントリ辞書を構築する。"""
    first_seen = _record_value(sorted_runs[-1], "createdAt", "created_at") if sorted_runs else None
    last_seen = _record_value(sorted_runs[0], "createdAt", "created_at") if sorted_runs else None
    session_ids = _unique_in_order(
        [sid for sid in (_record_value(run, "sessionId", "session_id") for run in sorted_runs) if sid]
    )
    versions = _unique_in_order(
        [v for v in (_record_value(run, "skillVersion", "skill_version") for run in sorted_runs) if v]
    )
    raw_reasons = _unique_in_order(
        [r for r in (_record_value(run, "failureReason", "failure_reason") for run in sorted_runs) if r]
    )
    return {
        "skillId": group["skillId"],
        "normalizedReason": group["normalizedReason"],
        "count": len(group["runs"]),
        "firstSeen": first_seen,
        "lastSeen": last_seen,
        "sessionIds": session_ids,
        "versions": versions,
        "rawReasons": raw_reasons,
        "runIds": [get_value(run, "id") for run in sorted_runs],
    }


def detect_patterns(
    skill_runs: list[Any], options: dict[str, Any] | None = None, /, **kwargs: Any
) -> list[dict[str, Any]]:
    """スキル実行から繰り返し発生する失敗パターンを検出する。

    Args:
        skill_runs: skill_runs の値
        options: オプション設定の辞書
        kwargs: kwargs の値

    Returns:
        list[dict[str, Any]]: dict[str, Any] の一覧を返します。

    Raises:
        例外は発生しません。
    """
    opts = merge_options(options, **kwargs)
    threshold = int(get_option(opts, "threshold", default=DEFAULT_FAILURE_THRESHOLD))
    groups = group_failures(skill_runs)
    patterns: list[dict[str, Any]] = []

    for group in groups.values():
        if len(group["runs"]) < threshold:
            continue
        sorted_runs = sorted(
            group["runs"],
            key=lambda run: _record_value(run, "createdAt", "created_at") or "",
            reverse=True,
        )
        patterns.append(_build_pattern_entry(group, sorted_runs))

    patterns.sort(key=lambda item: (item["count"], item["lastSeen"] or ""), reverse=True)
    return patterns


def suggest_action(pattern: dict[str, Any]) -> str:
    """パターンに対する修復アクションを提案する。

    Args:
        pattern: 検索パターン

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    reason = str(pattern.get("normalizedReason") or "")

    if "timeout" in reason:
        return "Increase timeout or optimize skill execution time."
    if "permission" in reason or "denied" in reason or "auth" in reason:
        return "Check tool permissions and authentication configuration."
    if "not found" in reason or "missing" in reason:
        return "Verify required files/dependencies exist before skill execution."
    if "parse" in reason or "syntax" in reason or "json" in reason:
        return "Review input/output format expectations and add validation."
    if len(pattern.get("versions", [])) > 1:
        return "Failure spans multiple versions. Consider rollback to last stable version."
    return "Investigate root cause and consider adding error handling."


def _format_pattern_for_report(pattern: dict[str, Any]) -> dict[str, Any]:
    """パターン辞書をレポート出力形式に変換する。"""
    return {
        "skillId": pattern["skillId"],
        "normalizedReason": pattern["normalizedReason"],
        "count": pattern["count"],
        "firstSeen": pattern["firstSeen"],
        "lastSeen": pattern["lastSeen"],
        "sessionIds": pattern["sessionIds"],
        "versions": pattern["versions"],
        "rawReasons": pattern["rawReasons"][:5],
        "suggestedAction": suggest_action(pattern),
    }


def generate_report(
    patterns: list[dict[str, Any]], options: dict[str, Any] | None = None, /, **kwargs: Any
) -> dict[str, Any]:
    """検査レポートを生成する。

    Args:
        patterns: 検索パターンの一覧
        options: オプション設定の辞書
        kwargs: kwargs の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    opts = merge_options(options, **kwargs)
    generated_at = get_option(opts, "generated_at", "generatedAt", default=None) or utc_now_iso()

    if not patterns:
        return {
            "generatedAt": generated_at,
            "status": "clean",
            "patternCount": 0,
            "patterns": [],
            "summary": "No recurring failure patterns detected.",
        }

    total_failures = sum(pattern["count"] for pattern in patterns)
    affected_skills = _unique_in_order([pattern["skillId"] for pattern in patterns])
    return {
        "generatedAt": generated_at,
        "status": "attention_needed",
        "patternCount": len(patterns),
        "totalFailures": total_failures,
        "affectedSkills": affected_skills,
        "patterns": [_format_pattern_for_report(p) for p in patterns],
        "summary": f"Found {len(patterns)} recurring failure pattern(s) across {len(affected_skills)} skill(s) ({total_failures} total failures).",
    }


def inspect(store: Any, options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """
    状態ストアに対して完全な検査パイプラインを実行する。

    Args:
        store: 状態ストア
        options: オプション設定の辞書
        kwargs: kwargs の値

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    opts = merge_options(options, **kwargs)
    window_size = int(get_option(opts, "window_size", "windowSize", default=DEFAULT_WINDOW_SIZE))
    threshold = int(get_option(opts, "threshold", default=DEFAULT_FAILURE_THRESHOLD))

    method = getattr(store, "get_status", None) or getattr(store, "getStatus", None)
    if method is None:
        raise AttributeError("store must provide get_status or getStatus")

    try:
        status = method(recent_skill_run_limit=window_size)
    except TypeError:
        status = method({"recentSkillRunLimit": window_size})

    skill_runs_container = get_value(status, "skillRuns", "skill_runs", default={})
    if isinstance(skill_runs_container, dict):
        skill_runs = skill_runs_container.get("recent", [])
    else:
        skill_runs = get_value(skill_runs_container, "recent", default=[])
    patterns = detect_patterns(skill_runs, threshold=threshold)
    generated_at = get_value(status, "generatedAt", "generated_at")
    return generate_report(patterns, generatedAt=generated_at)


__all__ = [
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_WINDOW_SIZE",
    "detect_patterns",
    "generate_report",
    "group_failures",
    "inspect",
    "normalize_failure_reason",
    "suggest_action",
]
