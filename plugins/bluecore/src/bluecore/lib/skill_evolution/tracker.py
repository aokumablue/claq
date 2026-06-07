"""スキル実行レコードの正規化・保存・読み出しを担当する。

このモジュールは、スキルの実行イベントを JSONL または state-store に
保存し、後続の健全性分析やダッシュボードが同じ形式で扱えるようにする。
入力値の検証と表現の統一もここで担う。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import append_file, get_bluecore_dir

from .skill_evolution_compat import get_option, get_value, merge_options, parse_iso_timestamp, utc_now_iso

VALID_OUTCOMES = {"success", "failure", "partial"}
VALID_FEEDBACK = {"accepted", "corrected", "rejected"}


def get_runs_file_path(options: dict[str, Any] | None = None, /, **kwargs: Any) -> str:
    """スキル実行レコード用 JSONL ファイルパスを解決する。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        実行レコード保存先の JSONL パス。

    Raises:
        なし。
    """
    opts = merge_options(options, **kwargs)
    runs_file_path = get_option(opts, "runs_file_path", "runsFilePath")
    # 明示的なパス指定があれば、その値を最優先で使う。
    if runs_file_path is not None:
        return str(Path(str(runs_file_path)).expanduser().resolve())

    # デフォルトは bluecore の状態ディレクトリ配下に保存する。
    return str(get_bluecore_dir() / "state" / "skill-runs.jsonl")


def to_nullable_number(value: Any, field_name: str) -> float | None:
    """値を数値または None に変換する。

    Args:
        value: 変換対象の値。
        field_name: エラーメッセージ用のフィールド名。

    Returns:
        数値化できた場合は float、None の場合は None。

    Raises:
        ValueError: 値が数値に変換できない場合。
    """
    # 未指定値はそのまま None として通す。
    if value is None:
        return None

    # bool は int のサブクラスだが、数値入力としては受け付けない。
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a number")

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be a number") from error

    # NaN は比較不能なので、明示的に拒否する。
    if numeric_value != numeric_value:  # NaN チェック
        raise ValueError(f"{field_name} must be a number")

    return numeric_value


def _to_state_store_payload(record: dict[str, Any]) -> dict[str, Any]:
    """正規化済み実行レコードを state-store 用 payload に変換する。

    Args:
        record: 正規化済み実行レコード。

    Returns:
        state-store が期待する camelCase 形式の辞書。

    Raises:
        なし。
    """
    # state-store 側の camelCase に合わせて、内部表現を変換する。
    return {
        "skillId": record["skill_id"],
        "skillVersion": record["skill_version"],
        "taskDescription": record["task_description"],
        "outcome": record["outcome"],
        "failureReason": record["failure_reason"],
        "tokensUsed": record["tokens_used"],
        "durationMs": record["duration_ms"],
        "userFeedback": record["user_feedback"],
        "recordedAt": record["recorded_at"],
    }


def _extract_record_fields(input: dict[str, Any], opts: dict[str, Any]) -> dict[str, Any]:
    """実行レコード辞書から正規化前のフィールド値を抽出する。

    Args:
        input: 実行レコードの元データ辞書。
        opts: マージ済みオプション辞書。

    Returns:
        抽出したフィールド値をまとめた辞書。

    Raises:
        なし。
    """
    recorded_at = get_value(input, "recorded_at", "recordedAt") or get_option(opts, "now")
    # recorded_at が無ければ、オプションの now または現在時刻を使う。
    if recorded_at is None:
        recorded_at = utc_now_iso()

    return {
        "skill_id": get_value(input, "skill_id", "skillId"),
        "skill_version": get_value(input, "skill_version", "skillVersion"),
        "task_description": get_value(input, "task_description", "task_attempted", "taskAttempted"),
        "outcome": input.get("outcome"),
        "recorded_at": recorded_at,
        "user_feedback": get_value(input, "user_feedback", "userFeedback"),
        "failure_reason": get_value(input, "failure_reason", "failureReason"),
        "tokens_used": get_value(input, "tokens_used", "tokensUsed"),
        "duration_ms": get_value(input, "duration_ms", "durationMs"),
    }


def _validate_record_fields(fields: dict[str, Any]) -> None:
    """抽出済みフィールドを検証する。

    Args:
        fields: _extract_record_fields が返すフィールド辞書。

    Returns:
        なし（検証のみ）。

    Raises:
        ValueError: 必須項目が欠けているか不正な値の場合。
    """
    skill_id = fields["skill_id"]
    skill_version = fields["skill_version"]
    task_description = fields["task_description"]
    outcome = fields["outcome"]
    user_feedback = fields["user_feedback"]
    recorded_at = fields["recorded_at"]

    # 必須項目は空文字や空白のみも拒否する。
    if not isinstance(skill_id, str) or skill_id.strip() == "":
        raise ValueError("skill_id is required")
    if not isinstance(skill_version, str) or skill_version.strip() == "":
        raise ValueError("skill_version is required")
    if not isinstance(task_description, str) or task_description.strip() == "":
        raise ValueError("task_description is required")
    # outcome は定義済みの状態だけを受け付ける。
    if outcome not in VALID_OUTCOMES:
        raise ValueError("outcome must be one of success, failure, or partial")
    # user_feedback は許可済み値または未指定だけを受け付ける。
    if user_feedback is not None and user_feedback not in VALID_FEEDBACK:
        raise ValueError("user_feedback must be accepted, corrected, rejected, or null")
    # recorded_at は ISO 文字列である必要がある。
    if parse_iso_timestamp(recorded_at) is None:
        raise ValueError("recorded_at must be an ISO timestamp")


def normalize_execution_record(
    input: Any,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """スキル実行レコードを正規化して検証する。

    Args:
        input: 実行レコードの元データ。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        内部表現に正規化された実行レコード。

    Raises:
        ValueError: 入力形式や必須項目、値形式が不正な場合。
    """
    opts = merge_options(options, **kwargs)

    # 入力が object でなければ、レコードとして扱えない。
    if not isinstance(input, dict):
        raise ValueError("skill execution payload must be an object")

    fields = _extract_record_fields(input, opts)
    _validate_record_fields(fields)

    return {
        "skill_id": fields["skill_id"],
        "skill_version": fields["skill_version"],
        "task_description": fields["task_description"],
        "outcome": fields["outcome"],
        "failure_reason": fields["failure_reason"],
        "tokens_used": to_nullable_number(fields["tokens_used"], "tokens_used"),
        "duration_ms": to_nullable_number(fields["duration_ms"], "duration_ms"),
        "user_feedback": fields["user_feedback"],
        "recorded_at": fields["recorded_at"],
    }


def read_jsonl(file_path: str | Path) -> list[dict[str, Any]]:
    """不正な行を読み飛ばしながら JSONL を読み込む。

    Args:
        file_path: JSONL ファイルパス。

    Returns:
        読み込んだ JSON オブジェクトのリスト。

    Raises:
        なし。
    """
    path = Path(file_path)
    # ファイルが存在しなければ、空の履歴として扱う。
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    # 1 行ずつ読み込み、JSON として解釈できるものだけ残す。
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 空行は記録対象ではないため読み飛ばす。
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 壊れた行は全体を止めずに無視する。
            continue
    return records


def record_skill_execution(
    input: Any,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """スキル実行イベントを永続化する。

    Args:
        input: 実行レコードの元データ。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        保存先と正規化済みレコードを含む結果辞書。

    Raises:
        ValueError: 入力が不正な場合。
    """
    opts = merge_options(options, **kwargs)
    record = normalize_execution_record(input, opts)

    state_store = get_option(opts, "state_store", "stateStore")
    # state-store が利用できる場合は、そちらを優先して保存する。
    if state_store is not None:
        # state-store の実装差分を吸収するため、複数のメソッド名を順に試す。
        method = (
            getattr(state_store, "record_skill_execution", None)
            or getattr(state_store, "recordSkillExecution", None)
            or getattr(state_store, "insert_skill_run", None)
            or getattr(state_store, "insertSkillRun", None)
        )
        # 保存メソッドが見つかった場合だけ実行する。
        if method is not None:
            method_name = getattr(method, "__name__", "")
            # insert 系メソッドは camelCase ペイロードを期待するため変換する。
            if method_name in {"insert_skill_run", "insertSkillRun"}:
                payload = _to_state_store_payload(record)
            # 通常の record 系メソッドには、そのままの正規化レコードを渡す。
            else:
                payload = record
            result = method(payload)
            return {"storage": "state-store", "record": record, "result": result}

    # state-store が使えない場合は JSONL に追記して永続化する。
    runs_file_path = get_runs_file_path(opts)
    append_file(runs_file_path, f"{json.dumps(record)}\n")
    return {"storage": "jsonl", "path": runs_file_path, "record": record}


def read_skill_execution_records(
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> list[Any]:
    """設定されたストレージからスキル実行レコードを読み込む。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        読み込んだ実行レコードのリスト。

    Raises:
        なし。
    """
    opts = merge_options(options, **kwargs)
    state_store = get_option(opts, "state_store", "stateStore")
    # state-store が利用できる場合は、その読み出し API を優先する。
    if state_store is not None:
        # 読み出し側も state-store 実装差分を順に吸収する。
        method = (
            getattr(state_store, "read_skill_execution_records", None)
            or getattr(state_store, "readSkillExecutionRecords", None)
            or getattr(state_store, "list_skill_execution_records", None)
            or getattr(state_store, "listSkillExecutionRecords", None)
        )
        # 読み出しメソッドが見つかった場合だけ呼び出す。
        if method is not None:
            return method()

    # state-store が無い場合は JSONL をそのまま読む。
    return read_jsonl(get_runs_file_path(opts))


__all__ = [
    "VALID_FEEDBACK",
    "VALID_OUTCOMES",
    "get_runs_file_path",
    "normalize_execution_record",
    "read_jsonl",
    "read_skill_execution_records",
    "record_skill_execution",
    "to_nullable_number",
]
