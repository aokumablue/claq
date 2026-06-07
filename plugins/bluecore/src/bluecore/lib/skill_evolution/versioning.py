"""スキルのバージョン履歴と進化ログを管理する。

このモジュールは、SKILL.md のスナップショット保存、復元、履歴一覧化、
および amendments / observations / inspections の追跡を担当する。
スキルの変化を後からたどれるようにするのが目的である。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import append_file, ensure_dir

from .provenance import normalize_skill_dir
from .skill_evolution_compat import merge_options, to_iso_string, utc_now_iso

VERSION_DIRECTORY_NAME = ".versions"
EVOLUTION_DIRECTORY_NAME = ".evolution"
EVOLUTION_LOG_TYPES = ["observations", "inspections", "amendments"]


def get_skill_file_path(skill_path: str | Path) -> Path:
    """スキルの SKILL.md パスを返す。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        SKILL.md の絶対パス。

    Raises:
        なし。
    """
    return normalize_skill_dir(skill_path) / "SKILL.md"


def ensure_skill_exists(skill_path: str | Path) -> Path:
    """SKILL.md が存在することを確認する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        存在確認済みの SKILL.md パス。

    Raises:
        FileNotFoundError: SKILL.md が存在しない場合。
    """
    skill_file_path = get_skill_file_path(skill_path)
    # SKILL.md が無ければ、以降の versioning 操作は行えない。
    if not skill_file_path.exists():
        raise FileNotFoundError(f"Skill file not found: {skill_file_path}")
    return skill_file_path


def get_versions_dir(skill_path: str | Path) -> Path:
    """スキルの versions ディレクトリを取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        versions ディレクトリのパス。

    Raises:
        なし。
    """
    return normalize_skill_dir(skill_path) / VERSION_DIRECTORY_NAME


def get_evolution_dir(skill_path: str | Path) -> Path:
    """スキルの evolution ディレクトリを取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        evolution ディレクトリのパス。

    Raises:
        なし。
    """
    return normalize_skill_dir(skill_path) / EVOLUTION_DIRECTORY_NAME


def get_evolution_log_path(skill_path: str | Path, log_type: str) -> Path:
    """指定した evolution ログのパスを取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        log_type: logs の種類。

    Returns:
        指定ログの JSONL パス。

    Raises:
        ValueError: log_type が未知の場合。
    """
    # 許可済みログ種別だけを受け入れる。
    if log_type not in EVOLUTION_LOG_TYPES:
        raise ValueError(f"Unknown evolution log type: {log_type}")
    # ログ種別ごとに jsonl を分け、用途別に追記しやすくする。
    return get_evolution_dir(skill_path) / f"{log_type}.jsonl"


def ensure_skill_versioning(skill_path: str | Path) -> dict[str, str]:
    """スキル用の versioning 構成を整える。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        versions_dir と evolution_dir を含む辞書。

    Raises:
        FileNotFoundError: SKILL.md が存在しない場合。
    """
    ensure_skill_exists(skill_path)
    versions_dir = get_versions_dir(skill_path)
    evolution_dir = get_evolution_dir(skill_path)

    # まだディレクトリが無ければ作成し、履歴保存の土台を整える。
    ensure_dir(versions_dir)
    ensure_dir(evolution_dir)

    # 必要な evolution ログをすべて初期化しておく。
    for log_type in EVOLUTION_LOG_TYPES:
        log_path = get_evolution_log_path(skill_path, log_type)
        # ログファイルが無い場合は空ファイルを作成しておく。
        if not log_path.exists():
            log_path.write_text("", encoding="utf-8")

    return {"versions_dir": str(versions_dir), "evolution_dir": str(evolution_dir)}


def parse_version_number(file_name: str) -> int | None:
    """スナップショットファイル名からバージョン番号を解析する。

    Args:
        file_name: ファイル名。

    Returns:
        解析できた場合はバージョン番号、そうでなければ None。

    Raises:
        なし。
    """
    # v<number>.md 以外は version スナップショットとして扱わない。
    if not file_name.startswith("v") or not file_name.endswith(".md"):
        return None

    number = file_name[1:-3]
    # 数字以外が混じる場合は無効とみなす。
    if not number.isdigit():
        return None
    # v<number>.md 形式だけを採用する。
    return int(number)


def list_versions(skill_path: str | Path) -> list[dict[str, Any]]:
    """スキルのバージョンスナップショット一覧を取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        version、path、created_at を含む一覧。

    Raises:
        なし。
    """
    versions_dir = get_versions_dir(skill_path)
    # versions ディレクトリが無ければ履歴は空とみなす。
    if not versions_dir.exists():
        return []

    versions: list[dict[str, Any]] = []
    # versions ディレクトリを走査し、命名規則に合うスナップショットだけ集める。
    for entry in versions_dir.iterdir():
        # ディレクトリや命名規則外のファイルは履歴として扱わない。
        if not entry.is_file():
            continue

        version = parse_version_number(entry.name)
        # 命名規則に合わないファイルは無視する。
        if version is None:
            continue

        stats = entry.stat()
        # 更新時刻を残して、いつ作成されたかを後から追跡できるようにする。
        versions.append(
            {
                "version": version,
                "path": str(entry),
                "created_at": to_iso_string(datetime.fromtimestamp(stats.st_mtime, tz=UTC)),
            }
        )

    versions.sort(key=lambda item: item["version"])
    return versions


def get_current_version(skill_path: str | Path) -> int:
    """スキルの現在バージョン番号を取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        現在のバージョン番号。未初期化なら 0 または 1。

    Raises:
        なし。
    """
    # SKILL.md 自体が無ければ version 管理も未開始とみなす。
    if not get_skill_file_path(skill_path).exists():
        return 0

    versions = list_versions(skill_path)
    # ファイルはあるがスナップショットが無い場合は初版扱い。
    if not versions:
        return 1
    return int(versions[-1]["version"])


def append_evolution_record(skill_path: str | Path, log_type: str, record: dict[str, Any]) -> dict[str, Any]:
    """evolution ログにレコードを追記する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        log_type: ログ種別。
        record: 追記するレコード。

    Returns:
        追記したレコードのコピー。

    Raises:
        FileNotFoundError: SKILL.md が存在しない場合。
        ValueError: log_type が未知の場合。
    """
    ensure_skill_versioning(skill_path)
    log_path = get_evolution_log_path(skill_path, log_type)
    # 追記のみで履歴を壊さないようにする。
    append_file(log_path, f"{json.dumps(record)}\n")
    return dict(record)


def read_jsonl(file_path: str | Path) -> list[dict[str, Any]]:
    """不正な行をスキップしながら JSONL を読み込む。

    Args:
        file_path: JSONL ファイルのパス。

    Returns:
        読み込んだ JSON オブジェクトのリスト。

    Raises:
        なし。
    """
    path = Path(file_path)
    # ファイルが無ければ、履歴がまだ無いものとして扱う。
    if not path.exists():
        return []

    records: list[dict[str, Any]] = []
    # 1 行ずつ読み込み、壊れた JSON は除外する。
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        # 空行は記録対象ではないため読み飛ばす。
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # 壊れた行は全体を止めずに読み飛ばす。
            continue
    return records


def get_evolution_log(skill_path: str | Path, log_type: str) -> list[dict[str, Any]]:
    """evolution ログを読み込む。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        log_type: ログ種別。

    Returns:
        読み込んだ JSON オブジェクトのリスト。

    Raises:
        ValueError: log_type が未知の場合。
    """
    return read_jsonl(get_evolution_log_path(skill_path, log_type))


def create_version(skill_path: str | Path, options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """スキルのバージョンスナップショットを作成する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        version、path、created_at を含む結果辞書。

    Raises:
        FileNotFoundError: SKILL.md が存在しない場合。
    """
    opts = merge_options(options, **kwargs)
    skill_file_path = ensure_skill_exists(skill_path)
    ensure_skill_versioning(skill_path)

    versions = list_versions(skill_path)
    # 既存の最新バージョンから次の番号を決める。
    next_version = 1 if not versions else int(versions[-1]["version"]) + 1
    snapshot_path = get_versions_dir(skill_path) / f"v{next_version}.md"
    created_at = opts.get("timestamp") or utc_now_iso()

    # 現在の SKILL.md をそのままスナップショットとして保存する。
    snapshot_path.write_text(skill_file_path.read_text(encoding="utf-8"), encoding="utf-8")
    append_evolution_record(
        skill_path,
        "amendments",
        {
            "event": "snapshot",
            "version": next_version,
            "reason": opts.get("reason") or None,
            "author": opts.get("author") or None,
            "status": "applied",
            "created_at": created_at,
        },
    )

    return {"version": next_version, "path": str(snapshot_path), "created_at": created_at}


def _validate_target_version(target_version: int | str) -> int:
    """ロールバック先バージョン番号を検証して正規化する。

    Args:
        target_version: 復元対象のバージョン番号または文字列。

    Returns:
        正規化済みの正の整数バージョン番号。

    Raises:
        ValueError: target_version が不正な場合。
    """
    # bool は数値として扱わず、明示的に拒否する。
    if isinstance(target_version, bool):
        raise ValueError(f"Invalid target version: {target_version}")

    try:
        normalized = float(target_version)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid target version: {target_version}") from error

    # 正の整数バージョンだけを許容する。
    if not normalized.is_integer() or normalized <= 0:
        raise ValueError(f"Invalid target version: {target_version}")

    return int(normalized)


def _apply_rollback(skill_path: str | Path, version_int: int, opts: dict[str, Any]) -> dict[str, Any]:
    """スナップショット復元と evolution ログ追記を実行する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        version_int: 復元対象の整数バージョン番号。
        opts: マージ済みオプション辞書。

    Returns:
        create_version の結果辞書。

    Raises:
        FileNotFoundError: 対象バージョンや SKILL.md が存在しない場合。
    """
    target_path = get_versions_dir(skill_path) / f"v{version_int}.md"
    # 対象スナップショットが無ければ、復元できない。
    if not target_path.exists():
        raise FileNotFoundError(f"Version not found: v{version_int}")

    current_version = get_current_version(skill_path)
    target_content = target_path.read_text(encoding="utf-8")
    # 対象バージョンの内容を SKILL.md に戻す。
    get_skill_file_path(skill_path).write_text(target_content, encoding="utf-8")

    # ロールバック後の状態を新しいバージョンとして記録する。
    created_version = create_version(
        skill_path,
        timestamp=opts.get("timestamp"),
        reason=opts.get("reason") or f"rollback to v{version_int}",
        author=opts.get("author"),
    )

    append_evolution_record(
        skill_path,
        "amendments",
        {
            "event": "rollback",
            "version": created_version["version"],
            "source_version": current_version,
            "target_version": version_int,
            "reason": opts.get("reason") or None,
            "author": opts.get("author") or None,
            "status": "applied",
            "created_at": opts.get("timestamp") or utc_now_iso(),
        },
    )

    return created_version


def rollback_to(
    skill_path: str | Path,
    target_version: int | str,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """指定バージョンへロールバックして新しいスナップショットを作成する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        target_version: 復元対象のバージョン。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        create_version の結果辞書。

    Raises:
        ValueError: target_version が数値として解釈できない場合。
        FileNotFoundError: 対象バージョンや SKILL.md が存在しない場合。
    """
    opts = merge_options(options, **kwargs)
    version_int = _validate_target_version(target_version)
    ensure_skill_exists(skill_path)
    ensure_skill_versioning(skill_path)
    return _apply_rollback(skill_path, version_int, opts)


__all__ = [
    "EVOLUTION_DIRECTORY_NAME",
    "EVOLUTION_LOG_TYPES",
    "VERSION_DIRECTORY_NAME",
    "append_evolution_record",
    "create_version",
    "ensure_skill_exists",
    "ensure_skill_versioning",
    "get_current_version",
    "get_evolution_dir",
    "get_evolution_log",
    "get_evolution_log_path",
    "get_skill_file_path",
    "get_versions_dir",
    "list_versions",
    "parse_version_number",
    "read_jsonl",
    "rollback_to",
]
