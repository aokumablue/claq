"""スキル進化モジュール向けの内部ヘルパー。"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any


def merge_options(options: dict[str, Any] | None = None, /, **kwargs: Any) -> dict[str, Any]:
    """オプションの辞書とキーワード引数をマージする。"""
    merged: dict[str, Any] = {}
    if options:
        merged.update(options)
    merged.update(kwargs)
    return merged


def get_option(options: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    """マッピングから最初に定義されたオプション値を取得する。"""
    for name in names:
        if name in options:
            value = options[name]
            if value is None:
                continue
            if isinstance(value, str) and value == "":
                continue
            return value
    return default


def get_value(item: Any, *names: str, default: Any = None) -> Any:
    """複数の名前を試して、マッピングまたはオブジェクトから値を取得する。"""
    if item is None:
        return default

    if isinstance(item, Mapping):
        for name in names:
            if name in item and item[name] is not None:
                return item[name]
        return default

    for name in names:
        if hasattr(item, name):
            value = getattr(item, name)
            if value is not None:
                return value

    return default


def utc_now_iso() -> str:
    """現在の UTC 時刻を ISO 8601 形式で返す。"""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_timestamp(value: Any) -> datetime | None:
    """ISO タイムスタンプを datetime に解析する。"""
    if not isinstance(value, str) or value.strip() == "":
        return None

    candidate = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def is_iso_timestamp(value: Any) -> bool:
    """値が ISO タイムスタンプかどうかを確認する。"""
    return parse_iso_timestamp(value) is not None


def to_iso_string(value: datetime) -> str:
    """datetime を UTC の ISO 文字列に変換する。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
