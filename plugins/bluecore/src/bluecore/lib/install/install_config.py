"""
bluecore-install.json の読み込みと検証を行います。
Pydantic スキーマで設定内容を確かめ、重複を取り除いた正規化済み設定を返します。
設定ファイルの既定パス探索も担当します。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError

DEFAULT_INSTALL_CONFIG = "bluecore-install.json"


class InstallConfigSchema(BaseModel):
    """bluecore-install.json 設定ファイルのスキーマ。"""

    version: int = Field(..., ge=1)
    target: str | None = None
    profile: str | None = None
    modules: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)
    options: dict[str, Any] = Field(default_factory=dict)

    model_config = {"extra": "forbid"}


@dataclass
class InstallConfig:
    """読み込み済みかつ検証済みのインストール設定。"""

    path: Path
    version: int
    target: str | None
    profile_id: str | None
    module_ids: list[str]
    include_component_ids: list[str]
    exclude_component_ids: list[str]
    options: dict[str, Any]


def _dedupe_strings(values: list[str] | None) -> list[str]:
    """文字列リストを重複排除し、整形する。

    Args:
        values: values の値

    Returns:
        list[str]: str の一覧を返します。

    Raises:
        例外は発生しません。
    """
    if not values:
        return []

    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            result.append(cleaned)
    return result


def _format_validation_errors(validation_error: ValidationError) -> str:
    """Pydantic の検証エラーを整形する。

    Args:
        validation_error: Pydantic の検証エラー

    Returns:
        検証エラーを 1 つの文字列に整形した結果。

    Raises:
        例外は発生しません。
    """
    errors = []
    for error in validation_error.errors():
        loc = error.get("loc", ())
        instance_path = "/" + "/".join(str(p) for p in loc) if loc else "/"
        message = error.get("msg", "validation error")
        errors.append(f"{instance_path} {message}")
    return "; ".join(errors)


def resolve_install_config_path(
    config_path: str | Path,
    *,
    cwd: str | Path | None = None,
) -> Path:
    """
    インストール設定パスを絶対パスに解決する。

    Args:
        config_path: 設定ファイルのパス。
        cwd: カレントディレクトリ。

    Returns:
        設定ファイルの絶対パスを表す Path オブジェクト。

    Raises:
        ValueError: config_path が空の場合。
    """
    if not config_path:
        raise ValueError("An install config path is required")

    path = Path(config_path)
    if path.is_absolute():
        return path

    # 相対パスは cwd 基準に解決し、呼び出し元ごとの差をなくす。
    base = Path(cwd) if cwd else Path.cwd()
    return (base / path).resolve()


def find_default_install_config_path(
    *,
    cwd: str | Path | None = None,
) -> Path | None:
    """カレントディレクトリ内の既定インストール設定ファイルを探す。

    Args:
        cwd: カレントディレクトリ。

    Returns:
        見つかった場合は Path、なければ None。

    Raises:
        例外は発生しません。
    """
    base = Path(cwd) if cwd else Path.cwd()
    candidate = base / DEFAULT_INSTALL_CONFIG

    return candidate if candidate.exists() else None


def load_install_config(
    config_path: str | Path,
    *,
    cwd: str | Path | None = None,
) -> InstallConfig:
    """
    インストール設定ファイルを読み込み、検証する。

    Args:
        config_path: 設定ファイルのパス。
        cwd: カレントディレクトリ。

    Returns:
        検証済み InstallConfig。

    Raises:
        FileNotFoundError: 設定ファイルが見つからない場合。
        ValueError: JSON が不正、または検証に失敗した場合。
    """
    resolved_path = resolve_install_config_path(config_path, cwd=cwd)

    if not resolved_path.exists():
        raise FileNotFoundError(f"Install config not found: {resolved_path}")

    # JSONを読み込んで解析
    try:
        raw = json.loads(resolved_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in {resolved_path.name}: {e}") from e

    # スキーマに対して検証
    try:
        validated = InstallConfigSchema.model_validate(raw)
    except ValidationError as e:
        raise ValueError(f"Invalid install config {resolved_path}: {_format_validation_errors(e)}") from e

    return InstallConfig(
        path=resolved_path,
        version=validated.version,
        target=validated.target,
        profile_id=validated.profile,
        module_ids=_dedupe_strings(validated.modules),
        include_component_ids=_dedupe_strings(validated.include),
        exclude_component_ids=_dedupe_strings(validated.exclude),
        options=dict(validated.options) if validated.options else {},
    )


__all__ = [
    "DEFAULT_INSTALL_CONFIG",
    "InstallConfig",
    "InstallConfigSchema",
    "find_default_install_config_path",
    "load_install_config",
    "resolve_install_config_path",
]
