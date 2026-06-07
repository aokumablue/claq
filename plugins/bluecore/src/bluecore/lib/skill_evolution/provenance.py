"""スキルの来歴情報と配置ルートを扱う。

このモジュールは、スキルが curated / learned / imported のどれに属するかを
判定し、必要なら provenance メタデータを読み書きする。学習由来のスキルに
対しては、どこから来たかを後から追跡できるようにする。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bluecore.lib.core_utils import ensure_dir

from .skill_evolution_compat import get_option, is_iso_timestamp, merge_options

PROVENANCE_FILE_NAME = ".provenance.json"
SKILL_TYPES = {
    "CURATED": "curated",
    "LEARNED": "learned",
    "IMPORTED": "imported",
    "UNKNOWN": "unknown",
}


def _default_repo_root() -> str:
    """このモジュールから見たリポジトリルートを返す。

    Args:
        なし。

    Returns:
        リポジトリルートの絶対パス。

    Raises:
        なし。
    """
    return str(Path(__file__).resolve().parents[4])


def resolve_repo_root(repo_root: str | Path | None = None) -> str:
    """リポジトリルートの絶対パスを解決する。

    Args:
        repo_root: 上書き用のリポジトリルート。

    Returns:
        解決済みのリポジトリルート。

    Raises:
        なし。
    """
    # 未指定ならコード位置から自動推定したルートを使う。
    if repo_root is None:
        return _default_repo_root()
    return str(Path(str(repo_root)).expanduser().resolve())


def resolve_home_dir(home_dir: str | Path | None = None) -> str:
    """ホームディレクトリの絶対パスを解決する。

    Args:
        home_dir: 上書き用のホームディレクトリ。

    Returns:
        解決済みのホームディレクトリ。

    Raises:
        なし。
    """
    # 未指定なら現在のユーザーホームを採用する。
    if home_dir is None:
        return str(Path.home())
    return str(Path(str(home_dir)).expanduser().resolve())


def normalize_skill_dir(skill_path: str | Path | None) -> Path:
    """スキルパスをディレクトリ表現へ正規化する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        スキルディレクトリの Path。

    Raises:
        ValueError: skill_path が未指定または空文字列の場合。
    """
    # 未指定や空文字列は、スキルパスとして受け付けない。
    if skill_path is None or str(skill_path).strip() == "":
        raise ValueError("skillPath is required")

    resolved_path = Path(str(skill_path)).expanduser().resolve()
    # SKILL.md が直接渡された場合は、その親ディレクトリを基準にする。
    if resolved_path.name == "SKILL.md":
        return resolved_path.parent
    return resolved_path


def _is_within_root(target_path: Path, root_path: Path) -> bool:
    """target_path が root_path 配下かどうかを判定する。

    Args:
        target_path: 判定対象パス。
        root_path: 基準ルート。

    Returns:
        配下にあれば True。

    Raises:
        なし。
    """
    try:
        target_path.relative_to(root_path)
        return True
    except ValueError:
        return False


def get_skill_roots(options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, str]:
    """スキル種別ごとのルートディレクトリを返す。

    Args:
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        curated、learned、imported の各ルートを含む辞書。

    Raises:
        なし。
    """
    opts = merge_options(options, **kwargs)
    repo_root = get_option(opts, "repo_root", "repoRoot")
    home_dir = get_option(opts, "home_dir", "homeDir")

    resolved_repo_root = resolve_repo_root(repo_root)
    resolved_home_dir = resolve_home_dir(home_dir)

    return {
        # curated はリポジトリ内の同梱スキルを指す。
        "curated": str(Path(resolved_repo_root) / "skills"),
        # learned / imported はユーザーのホーム配下に置く。
        "learned": str(Path(resolved_home_dir) / ".claude" / "skills" / "learned"),
        "imported": str(Path(resolved_home_dir) / ".claude" / "skills" / "imported"),
    }


def classify_skill_path(
    skill_path: str | Path,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> str:
    """スキルパスを curated・learned・imported・unknown に分類する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        スキル種別文字列。

    Raises:
        なし。
    """
    skill_dir = normalize_skill_dir(skill_path)
    roots = get_skill_roots(options, **kwargs)

    # より狭いルートから順に照合し、最初に一致した種別を返す。
    if _is_within_root(skill_dir, Path(roots["curated"]).resolve()):
        return SKILL_TYPES["CURATED"]
    # curated に該当しない場合は learned ルートを確認する。
    if _is_within_root(skill_dir, Path(roots["learned"]).resolve()):
        return SKILL_TYPES["LEARNED"]
    # learned にも該当しない場合は imported ルートを確認する。
    if _is_within_root(skill_dir, Path(roots["imported"]).resolve()):
        return SKILL_TYPES["IMPORTED"]
    return SKILL_TYPES["UNKNOWN"]


def requires_provenance(
    skill_path: str | Path,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> bool:
    """スキルに provenance メタデータが必要かどうかを判定する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        learned または imported の場合は True。

    Raises:
        なし。
    """
    skill_type = classify_skill_path(skill_path, options, **kwargs)
    # 学習・インポート由来のスキルだけ来歴情報を要求する。
    return skill_type in {SKILL_TYPES["LEARNED"], SKILL_TYPES["IMPORTED"]}


def get_provenance_path(skill_path: str | Path) -> Path:
    """スキルの provenance ファイルパスを取得する。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。

    Returns:
        provenance ファイルの Path。

    Raises:
        なし。
    """
    return normalize_skill_dir(skill_path) / PROVENANCE_FILE_NAME


def validate_provenance(record: Any) -> dict[str, Any]:
    """provenance レコードを検証する。

    Args:
        record: 検証対象の provenance レコード。

    Returns:
        valid と errors を含む検証結果。

    Raises:
        なし。
    """
    errors: list[str] = []

    # オブジェクト以外はレコードとして扱わない。
    if not isinstance(record, dict):
        return {"valid": False, "errors": ["provenance record must be an object"]}

    source = record.get("source")
    # source は空文字列を許さず、来歴の出所として必須にする。
    if not isinstance(source, str) or source.strip() == "":
        errors.append("source is required")

    # created_at は ISO タイムスタンプである必要がある。
    if not is_iso_timestamp(record.get("created_at")):
        errors.append("created_at must be an ISO timestamp")

    confidence = record.get("confidence")
    # confidence は 0〜1 の数値として扱う。
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        errors.append("confidence must be a number")
    # 数値として妥当でも、0〜1 の範囲外なら無効とする。
    elif confidence < 0 or confidence > 1:
        errors.append("confidence must be between 0 and 1")

    author = record.get("author")
    # author も provenance の説明責任に必要な必須項目とする。
    if not isinstance(author, str) or author.strip() == "":
        errors.append("author is required")

    return {"valid": len(errors) == 0, "errors": errors}


def assert_valid_provenance(record: Any) -> None:
    """provenance レコードが不正なら例外を送出する。

    Args:
        record: 検証対象の provenance レコード。

    Returns:
        None。

    Raises:
        ValueError: provenance レコードが不正な場合。
    """
    validation = validate_provenance(record)
    # 検証結果が不正なら、まとめたエラーメッセージで例外化する。
    if not validation["valid"]:
        raise ValueError(f"Invalid provenance metadata: {'; '.join(validation['errors'])}")


def read_provenance(
    skill_path: str | Path,
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any] | None:
    """スキルディレクトリから provenance メタデータを読み込む。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        provenance レコード。未設定かつ任意なら None。

    Raises:
        ValueError: 必須なのに provenance が無い場合、または内容が不正な場合。
        json.JSONDecodeError: provenance ファイルが JSON として解釈できない場合。
    """
    opts = merge_options(options, **kwargs)
    skill_dir = normalize_skill_dir(skill_path)
    provenance_path = get_provenance_path(skill_dir)
    required = get_option(opts, "required", default=False)
    # required オプションか、配置ルール上の必須条件かで判定する。
    provenance_required = (required is True) or requires_provenance(skill_dir, opts)

    # ファイルが無い場合は、必要かどうかで挙動を分ける。
    if not provenance_path.exists():
        # 必須対象なら例外にし、任意対象なら未設定として扱う。
        if provenance_required:
            raise ValueError(f"Missing provenance metadata for {skill_dir}")
        return None

    record = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert_valid_provenance(record)
    return record


def write_provenance(
    skill_path: str | Path,
    record: dict[str, Any],
    options: dict[str, Any] | None = None,
    /,
    **kwargs: Any,
) -> dict[str, Any]:
    """スキルの provenance メタデータを書き込む。

    Args:
        skill_path: スキルディレクトリまたは SKILL.md のパス。
        record: 書き込む provenance レコード。
        options: オプション辞書。
        **kwargs: 追加オプション。

    Returns:
        path と record を含む辞書。

    Raises:
        ValueError: provenance が必要なスキル以外に書き込もうとした場合、または record が不正な場合。
    """
    opts = merge_options(options, **kwargs)
    skill_dir = normalize_skill_dir(skill_path)

    # learned/imported 以外への書き込みは禁止する。
    if not requires_provenance(skill_dir, opts):
        raise ValueError(f"Provenance metadata is only required for learned or imported skills: {skill_dir}")

    assert_valid_provenance(record)
    # 保存先ディレクトリが無ければ作成してから書き込む。
    ensure_dir(skill_dir)

    provenance_path = get_provenance_path(skill_dir)
    provenance_path.write_text(f"{json.dumps(record, indent=2)}\n", encoding="utf-8")
    return {"path": str(provenance_path), "record": dict(record)}


__all__ = [
    "PROVENANCE_FILE_NAME",
    "SKILL_TYPES",
    "assert_valid_provenance",
    "classify_skill_path",
    "get_provenance_path",
    "get_skill_roots",
    "normalize_skill_dir",
    "read_provenance",
    "requires_provenance",
    "resolve_home_dir",
    "resolve_repo_root",
    "validate_provenance",
    "write_provenance",
]
