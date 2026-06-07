"""
インストール状態 JSON の生成、検証、入出力を扱う。
Pydantic モデルを使ってスキーマ整合性を保ち、JS 側の期待する構造に寄せた辞書を返す。
検証エラーの整形とファイル保存をこのモジュールに集約する。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator


class InstallTarget(BaseModel):
    """インストール先の場所。"""

    id: str = Field(..., min_length=1)
    target: str | None = None
    kind: Literal["home", "project"] | None = None
    root: str = Field(..., min_length=1)
    installStatePath: str = Field(..., min_length=1, alias="install_state_path")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class InstallRequest(BaseModel):
    """インストール要求の設定。"""

    profile: str | None = None
    modules: list[str] = Field(default_factory=list)
    includeComponents: list[str] = Field(default_factory=list, alias="include_components")
    excludeComponents: list[str] = Field(default_factory=list, alias="exclude_components")
    legacyLanguages: list[str] = Field(default_factory=list, alias="legacy_languages")
    legacyMode: bool = Field(default=False, alias="legacy_mode")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("modules", "includeComponents", "excludeComponents", "legacyLanguages")
    @classmethod
    def validate_string_list(cls, v: list[str]) -> list[str]:
        """
        文字列リストの各要素を検証する。

        Args:
            v: 検証対象の値一覧。

        Returns:
            list[str]: 検証済みの値一覧。

        Raises:
            ValueError: 空文字列や文字列以外が含まれる場合。
        """
        for item in v:
            if not isinstance(item, str) or len(item) == 0:
                raise ValueError("must be non-empty string")
        return v


class InstallResolution(BaseModel):
    """モジュール選択の解決結果。"""

    selectedModules: list[str] = Field(default_factory=list, alias="selected_modules")
    skippedModules: list[str] = Field(default_factory=list, alias="skipped_modules")

    model_config = {"populate_by_name": True, "extra": "forbid"}

    @field_validator("selectedModules", "skippedModules")
    @classmethod
    def validate_string_list(cls, v: list[str]) -> list[str]:
        """
        文字列リストの各要素を検証する。

        Args:
            v: 検証対象の値一覧。

        Returns:
            list[str]: 検証済みの値一覧。

        Raises:
            ValueError: 空文字列や文字列以外が含まれる場合。
        """
        for item in v:
            if not isinstance(item, str) or len(item) == 0:
                raise ValueError("must be non-empty string")
        return v


class InstallSource(BaseModel):
    """ソースリポジトリ情報。"""

    repoVersion: str | None = Field(default=None, alias="repo_version")
    repoCommit: str | None = Field(default=None, alias="repo_commit")
    manifestVersion: int = Field(..., ge=1, alias="manifest_version")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class InstallOperation(BaseModel):
    """単一のインストール操作。"""

    kind: str = Field(..., min_length=1)
    moduleId: str = Field(..., min_length=1, alias="module_id")
    sourceRelativePath: str = Field(..., min_length=1, alias="source_relative_path")
    destinationPath: str = Field(..., min_length=1, alias="destination_path")
    strategy: str = Field(..., min_length=1)
    ownership: str = Field(..., min_length=1)
    scaffoldOnly: bool = Field(..., alias="scaffold_only")

    model_config = {"populate_by_name": True, "extra": "forbid"}


class InstallState(BaseModel):
    """完全なインストール状態ドキュメント。"""

    schemaVersion: Literal["bluecore.install.v1"] = Field(..., alias="schema_version")
    installedAt: str = Field(..., min_length=1, alias="installed_at")
    lastValidatedAt: str | None = Field(default=None, alias="last_validated_at")
    target: InstallTarget
    request: InstallRequest
    resolution: InstallResolution
    source: InstallSource
    operations: list[InstallOperation] = Field(default_factory=list)

    model_config = {"populate_by_name": True, "extra": "forbid"}


class ValidationResult:
    """検証結果。"""

    def __init__(self, valid: bool, errors: list[dict[str, Any]] | None = None):
        """検証結果オブジェクトを初期化する。

        Args:
            valid: 検証結果の真偽値。
            errors: 検証エラー一覧。

        Returns:
            None: 値を返しません。

        Raises:
            例外は発生しません。
        """
        self.valid = valid
        self.errors = errors or []


def _pydantic_errors_to_ajv_format(validation_error: ValidationError) -> list[dict[str, Any]]:
    """Pydantic検証エラーをajv風フォーマットへ変換する。

    Args:
        validation_error: Pydantic の検証エラー

    Returns:
        list[dict[str, Any]]: dict[str, Any] の一覧を返します。

    Raises:
        例外は発生しません。
    """
    errors = []
    for error in validation_error.errors():
        loc = error.get("loc", ())
        instance_path = "/" + "/".join(str(p) for p in loc) if loc else "/"
        errors.append(
            {
                "instancePath": instance_path,
                "message": error.get("msg", "validation error"),
            }
        )
    return errors


def format_validation_errors(errors: list[dict[str, Any]]) -> str:
    """検証エラーを文字列として整形する。

    Args:
        errors: 検証エラー一覧

    Returns:
        str: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    return "; ".join(f"{error.get('instancePath', '/')} {error.get('message', '')}" for error in errors)


def validate_install_state(state: dict[str, Any]) -> ValidationResult:
    """インストール状態の辞書を検証する。

    Args:
        state: 検証対象の状態辞書。

    Returns:
        検証結果を表す ValidationResult。

    Raises:
        例外は発生しません。
    """
    try:
        # Pydantic の検証をそのまま使い、JS 側と同じ構造制約を保つ。
        InstallState.model_validate(state)
        return ValidationResult(valid=True)
    except ValidationError as e:
        return ValidationResult(valid=False, errors=_pydantic_errors_to_ajv_format(e))


def assert_valid_install_state(state: dict[str, Any], label: str | None = None) -> None:
    """
    インストール状態が有効であることを検証する。

    Args:
        state: 状態データ
        label: label の値

    Returns:
        None: 値を返しません。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    result = validate_install_state(state)
    if not result.valid:
        label_part = f" ({label})" if label else ""
        raise ValueError(f"Invalid install-state{label_part}: {format_validation_errors(result.errors)}")


def _clone_json_value(value: Any) -> Any:
    """JSONシリアライズ可能な値を深いコピーで複製する。

    Args:
        value: value の値

    Returns:
        Any: 処理結果を返します。

    Raises:
        例外は発生しません。
    """
    if value is None:
        return None
    return json.loads(json.dumps(value))


def _build_install_state_target(adapter: dict[str, Any], target_root: str, install_state_path: str) -> dict[str, Any]:
    """インストール先 target 辞書を構築し、None フィールドを除去する。"""
    target: dict[str, Any] = {
        "id": adapter.get("id"),
        "target": adapter.get("target"),
        "kind": adapter.get("kind"),
        "root": target_root,
        "installStatePath": install_state_path,
    }
    if target["target"] is None:
        del target["target"]
    if target["kind"] is None:
        del target["kind"]
    return target


def _build_install_state_request(request: dict[str, Any]) -> dict[str, Any]:
    """インストール要求 request 辞書を正規化して返す。"""
    return {
        "profile": request.get("profile"),
        "modules": list(request.get("modules", [])),
        "includeComponents": list(request.get("includeComponents", [])),
        "excludeComponents": list(request.get("excludeComponents", [])),
        "legacyLanguages": list(request.get("legacyLanguages", [])),
        "legacyMode": bool(request.get("legacyMode", False)),
    }


def create_install_state(  # noqa: PLR0913
    *,
    adapter: dict[str, Any],
    target_root: str,
    install_state_path: str,
    request: dict[str, Any],
    resolution: dict[str, Any],
    source: dict[str, Any],
    operations: list[dict[str, Any]] | None = None,
    installed_at: str | None = None,
    last_validated_at: str | None = None,
) -> dict[str, Any]:
    """新しいインストール状態辞書を作成する。

    Args:
        adapter: アダプター辞書
        target_root: ターゲットルートパス
        install_state_path: インストール状態ファイルのパス
        request: インストールリクエスト辞書
        resolution: 解決結果辞書
        source: ソース情報辞書
        operations: インストール操作リスト（省略可）
        installed_at: インストール日時 ISO 文字列（省略時は現在時刻）
        last_validated_at: 最終検証日時（省略可）
    """
    if installed_at is None:
        installed_at = datetime.now().isoformat()

    state: dict[str, Any] = {
        "schemaVersion": "bluecore.install.v1",
        "installedAt": installed_at,
        "target": _build_install_state_target(adapter, target_root, install_state_path),
        "request": _build_install_state_request(request),
        "resolution": {
            "selectedModules": list(resolution.get("selectedModules", [])),
            "skippedModules": list(resolution.get("skippedModules", [])),
        },
        "source": {
            "repoVersion": source.get("repoVersion"),
            "repoCommit": source.get("repoCommit"),
            "manifestVersion": source.get("manifestVersion"),
        },
        "operations": [_clone_json_value(op) for op in (operations or [])],
    }

    if last_validated_at:
        state["lastValidatedAt"] = last_validated_at

    assert_valid_install_state(state, "create")
    return state


def read_install_state(file_path: str | Path) -> dict[str, Any]:
    """
    インストール状態ファイルを読み込み、検証する。

    Args:
        file_path: ファイルパス

    Returns:
        dict[str, Any]: 処理結果を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    path = Path(file_path)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to read install-state: {e}") from e

    assert_valid_install_state(state, str(file_path))
    return state


def write_install_state(file_path: str | Path, state: dict[str, Any]) -> dict[str, Any]:
    """インストール状態ファイルを検証して書き込む。

    Args:
        file_path: ファイルパス
        state: 状態データ

    Returns:
        書き込んだ状態データ（引数 state をそのまま返す）。

    Raises:
        ValueError: state が install-state スキーマに適合しない場合。
    """
    assert_valid_install_state(state, str(file_path))

    path = Path(file_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    return state


__all__ = [
    "InstallOperation",
    "InstallRequest",
    "InstallResolution",
    "InstallSource",
    "InstallState",
    "InstallTarget",
    "ValidationResult",
    "assert_valid_install_state",
    "create_install_state",
    "format_validation_errors",
    "read_install_state",
    "validate_install_state",
    "write_install_state",
]
