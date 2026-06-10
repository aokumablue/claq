"""
インストールターゲット用ヘルパー。

インストールターゲットアダプター作成のための補助関数を提供する。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def normalize_relative_path(relative_path: str | None) -> str:
    """
    相対パスを正規化する。

    Args:
        relative_path: 正規化するパス

    Returns:
        スラッシュ区切りに正規化したパス
    """
    path_str = str(relative_path) if relative_path else ""
    # バックスラッシュをスラッシュに置換
    path_str = path_str.replace("\\", "/")
    # 先頭の ./ を除去
    while path_str.startswith("./"):
        path_str = path_str[2:]
    # 末尾のスラッシュを除去
    path_str = path_str.rstrip("/")
    return path_str


def resolve_base_root(
    scope: str, *, home_dir: str | None = None, project_root: str | None = None, repo_root: str | None = None
) -> str:
    """
    指定スコープのベースルートディレクトリを解決する。

    Args:
        scope: 'home' または 'project'
        home_dir: ホームディレクトリの上書き値
        project_root: プロジェクトルートディレクトリ
        repo_root: リポジトリルートディレクトリ（project_root の別名）

    Returns:
        ベースルートパス

    Raises:
        ValueError: scope が不正、または必要なパスが不足している場合
    """
    if scope == "home":
        return home_dir or os.path.expanduser("~")

    if scope == "project":
        root = project_root or repo_root
        if not root:
            raise ValueError("projectRoot or repoRoot is required for project install targets")
        return root

    raise ValueError(f"Unsupported install target scope: {scope}")


@dataclass
class ValidationIssue:
    """検証時の問題。"""

    severity: str
    code: str
    message: str
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換する。"""
        result = {
            "severity": self.severity,
            "code": self.code,
            "message": self.message,
        }
        result.update(self.extra)
        return result


def build_validation_issue(
    severity: str,
    code: str,
    message: str,
    **extra: Any,
) -> ValidationIssue:
    """
    検証問題を構築する。

    Args:
        severity: 問題の重大度（error, warning, info）
        code: 問題コード
        message: 問題メッセージ
        **extra: 追加フィールド

    Returns:
        ValidationIssue インスタンス
    """
    return ValidationIssue(severity=severity, code=code, message=message, extra=extra)


@dataclass
class ManagedOperation:
    """管理対象のファイル操作。"""

    kind: str = "copy-path"
    module_id: str | None = None
    source_relative_path: str = ""
    source_path: str | None = None
    destination_path: str = ""
    strategy: str = "preserve-relative-path"
    ownership: str = "managed"
    scaffold_only: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """辞書に変換する。"""
        result = {
            "kind": self.kind,
            "moduleId": self.module_id,
            "sourceRelativePath": self.source_relative_path,
            "destinationPath": self.destination_path,
            "strategy": self.strategy,
            "ownership": self.ownership,
            "scaffoldOnly": self.scaffold_only,
        }
        if self.source_path:
            result["sourcePath"] = self.source_path
        result.update(self.extra)
        return result


def create_managed_operation(  # noqa: PLR0913
    *,
    kind: str = "copy-path",
    module_id: str | None = None,
    source_relative_path: str = "",
    source_path: str | None = None,
    destination_path: str = "",
    strategy: str = "preserve-relative-path",
    ownership: str = "managed",
    scaffold_only: bool = True,
    **extra: Any,
) -> ManagedOperation:
    """
    管理対象のファイル操作を作成する。

    Args:
        kind: 操作種別
        module_id: モジュールID
        source_relative_path: ソースからの相対パス
        source_path: ソースの完全パス
        destination_path: 宛先パス
        strategy: コピー戦略
        ownership: ファイル所有区分
        scaffold_only: スキャフォールドのみかどうか
        **extra: 追加フィールド

    Returns:
        ManagedOperation インスタンス
    """
    return ManagedOperation(
        kind=kind,
        module_id=module_id,
        source_relative_path=normalize_relative_path(source_relative_path),
        source_path=source_path,
        destination_path=destination_path,
        strategy=strategy,
        ownership=ownership,
        scaffold_only=scaffold_only,
        extra=extra,
    )


@dataclass(frozen=True)
class ScaffoldParams:
    """create_scaffold_operation の位置・パス引数。"""

    module_id: str
    source_relative_path: str
    source_root: str | None = None
    repo_root: str | None = None
    home_dir: str | None = None
    project_root: str | None = None


@dataclass(frozen=True)
class PlanParams:
    """plan_operations のオプション引数。"""

    modules: list[dict] | None = None
    module: dict | None = None
    source_root: str | None = None
    repo_root: str | None = None
    home_dir: str | None = None
    project_root: str | None = None


@dataclass
class InstallTargetConfig:
    """インストールターゲットアダプター設定。"""

    id: str
    target: str
    kind: str
    root_segments: list[str]
    install_state_path_segments: list[str]
    native_root_relative_path: str | None = None
    plan_operations: Callable | None = None
    validate: Callable | None = None


class InstallTargetAdapter:
    """インストールターゲット用アダプター。"""

    def __init__(self, config: InstallTargetConfig):
        """設定を受け取りアダプターを初期化する。"""
        self._config = config
        self.id = config.id
        self.target = config.target
        self.kind = config.kind
        self.native_root_relative_path = config.native_root_relative_path

    def supports(self, target: str) -> bool:
        """このアダプターが指定ターゲットをサポートするか確認する。"""
        return target == self._config.target or target == self._config.id

    def resolve_root(
        self,
        *,
        home_dir: str | None = None,
        project_root: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        """このターゲットのルートディレクトリを解決する。"""
        base_root = resolve_base_root(
            self._config.kind,
            home_dir=home_dir,
            project_root=project_root,
            repo_root=repo_root,
        )
        return str(Path(base_root).joinpath(*self._config.root_segments))

    def get_install_state_path(
        self,
        *,
        home_dir: str | None = None,
        project_root: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        """インストール状態ファイルのパスを取得する。"""
        root = self.resolve_root(
            home_dir=home_dir,
            project_root=project_root,
            repo_root=repo_root,
        )
        return str(Path(root).joinpath(*self._config.install_state_path_segments))

    def resolve_destination_path(
        self,
        source_relative_path: str,
        *,
        home_dir: str | None = None,
        project_root: str | None = None,
        repo_root: str | None = None,
    ) -> str:
        """ソースファイルの宛先パスを解決する。"""
        normalized = normalize_relative_path(source_relative_path)
        target_root = self.resolve_root(
            home_dir=home_dir,
            project_root=project_root,
            repo_root=repo_root,
        )

        if self._config.native_root_relative_path and normalized == normalize_relative_path(
            self._config.native_root_relative_path
        ):
            return target_root

        return str(Path(target_root) / normalized)

    def determine_strategy(self, source_relative_path: str) -> str:
        """ソースファイルのコピー戦略を決定する。"""
        normalized = normalize_relative_path(source_relative_path)

        if self._config.native_root_relative_path and normalized == normalize_relative_path(
            self._config.native_root_relative_path
        ):
            return "sync-root-children"

        return "preserve-relative-path"

    def create_scaffold_operation(self, params: ScaffoldParams) -> ManagedOperation:
        """モジュールファイル用のスキャフォールド操作を作成する。"""
        normalized = normalize_relative_path(params.source_relative_path)
        root = params.source_root or params.repo_root
        source_path = str(Path(root) / normalized) if root else normalized

        return create_managed_operation(
            module_id=params.module_id,
            source_relative_path=normalized,
            source_path=source_path,
            destination_path=self.resolve_destination_path(
                normalized,
                home_dir=params.home_dir,
                project_root=params.project_root,
                repo_root=params.repo_root,
            ),
            strategy=self.determine_strategy(normalized),
        )

    def _scaffold_module_paths(
        self,
        mod: dict,
        *,
        source_root: str | None,
        repo_root: str | None,
        home_dir: str | None,
        project_root: str | None,
    ) -> list[ManagedOperation]:
        """単一モジュールの paths からスキャフォールド操作リストを生成する。"""
        paths = mod.get("paths", [])
        if not isinstance(paths, list):
            return []
        return [
            self.create_scaffold_operation(ScaffoldParams(
                module_id=mod.get("id", ""),
                source_relative_path=path,
                source_root=source_root,
                repo_root=repo_root,
                home_dir=home_dir,
                project_root=project_root,
            ))
            for path in paths
        ]

    def plan_operations(self, params: PlanParams | None = None) -> list[ManagedOperation]:
        """指定モジュールの操作計画を立てる。"""
        p = params or PlanParams()
        if self._config.plan_operations:
            return self._config.plan_operations(
                modules=p.modules,
                module=p.module,
                source_root=p.source_root,
                repo_root=p.repo_root,
                home_dir=p.home_dir,
                project_root=p.project_root,
                adapter=self,
            )

        kwargs = {"source_root": p.source_root, "repo_root": p.repo_root, "home_dir": p.home_dir, "project_root": p.project_root}

        if p.modules:
            operations: list[ManagedOperation] = []
            for mod in p.modules:
                operations.extend(self._scaffold_module_paths(mod, **kwargs))
            return operations

        if p.module:
            return self._scaffold_module_paths(p.module, **kwargs)

        return []

    def validate(
        self,
        *,
        home_dir: str | None = None,
        project_root: str | None = None,
        repo_root: str | None = None,
    ) -> list[ValidationIssue]:
        """このアダプターへの入力を検証する。"""
        if self._config.validate:
            return self._config.validate(
                home_dir=home_dir,
                project_root=project_root,
                repo_root=repo_root,
                adapter=self,
            )

        issues: list[ValidationIssue] = []

        if self._config.kind == "project" and not project_root and not repo_root:
            issues.append(
                build_validation_issue(
                    "error",
                    "missing-project-root",
                    "projectRoot or repoRoot is required for project install targets",
                )
            )

        # home ターゲットは resolve_base_root が homeDir 未指定でも
        # os.path.expanduser("~") で常に解決するため検証不要。

        return issues


def create_install_target_adapter(config: InstallTargetConfig) -> InstallTargetAdapter:
    """
    インストールターゲットアダプターを作成する。

    Args:
        config: アダプター設定

    Returns:
        InstallTargetAdapter インスタンス
    """
    return InstallTargetAdapter(config)


__all__ = [
    "InstallTargetAdapter",
    "InstallTargetConfig",
    "ManagedOperation",
    "ValidationIssue",
    "build_validation_issue",
    "create_install_target_adapter",
    "create_managed_operation",
    "normalize_relative_path",
    "resolve_base_root",
]
