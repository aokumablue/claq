"""
フックの有効/無効と無効化フラグを解決します。
固定の strict 判定とプロファイル一致だけを扱う共通層です。
"""

from __future__ import annotations

VALID_PROFILES = frozenset(["minimal", "standard", "strict"])


def normalize_id(value: str | None) -> str:
    """フック ID を小文字のトリム済み文字列に正規化する。

    Args:
        value: 値

    Returns:
        str: 文字列を返します。

    Raises:
        例外は発生しません。
    """
    return str(value or "").strip().lower()


def get_hook_profile() -> str:
    """常に strict を返す。"""
    return "strict"


def parse_profiles(
    raw_profiles: str | list[str] | None,
    fallback: list[str] | None = None,
) -> list[str]:
    """プロファイル指定を有効なプロファイルのリストに変換する。

    Args:
        raw_profiles: raw profile の一覧
        fallback: フォールバック値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    if fallback is None:
        fallback = ["standard", "strict"]

    if not raw_profiles:
        return list(fallback)

    if isinstance(raw_profiles, list):
        parsed = [str(v or "").strip().lower() for v in raw_profiles]
        parsed = [v for v in parsed if v in VALID_PROFILES]
        return parsed if parsed else list(fallback)

    parsed = [v.strip().lower() for v in str(raw_profiles).split(",")]
    parsed = [v for v in parsed if v in VALID_PROFILES]
    return parsed if parsed else list(fallback)


def is_hook_enabled(
    hook_id: str,
    *,
    profiles: str | list[str] | None = None,
) -> bool:
    """固定の strict プロファイルに基づき、フックが有効かどうかを確認する。

    Args:
        hook_id: フックID
        profiles: フック設定の profile 一覧

    Returns:
        bool: 条件を満たす場合は True、そうでない場合は False。

    Raises:
        例外は発生しません。
    """
    normalized = normalize_id(hook_id)
    if not normalized:
        return True

    profile = get_hook_profile()
    allowed_profiles = parse_profiles(profiles)
    return profile in allowed_profiles


__all__ = [
    "VALID_PROFILES",
    "normalize_id",
    "get_hook_profile",
    "parse_profiles",
    "is_hook_enabled",
]
