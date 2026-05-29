"""
パッケージマネージャーを検出し、実行コマンドを組み立てます。
package.json、ロックファイル、設定ファイル、環境変数の順で優先度を付けて解決します。
npm、pnpm、yarn、bun を共通のインターフェースで扱うための層です。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from devgear.lib.core_utils import command_exists, get_devgear_dir, read_file, write_file


@dataclass
class PackageManagerConfig:
    """パッケージマネージャーの設定。"""

    name: str
    lock_file: str
    install_cmd: str
    run_cmd: str
    exec_cmd: str
    test_cmd: str
    build_cmd: str
    dev_cmd: str


PACKAGE_MANAGERS: dict[str, PackageManagerConfig] = {
    "npm": PackageManagerConfig(
        name="npm",
        lock_file="package-lock.json",
        install_cmd="npm install",
        run_cmd="npm run",
        exec_cmd="npx",
        test_cmd="npm test",
        build_cmd="npm run build",
        dev_cmd="npm run dev",
    ),
    "pnpm": PackageManagerConfig(
        name="pnpm",
        lock_file="pnpm-lock.yaml",
        install_cmd="pnpm install",
        run_cmd="pnpm",
        exec_cmd="pnpm dlx",
        test_cmd="pnpm test",
        build_cmd="pnpm build",
        dev_cmd="pnpm dev",
    ),
    "yarn": PackageManagerConfig(
        name="yarn",
        lock_file="yarn.lock",
        install_cmd="yarn",
        run_cmd="yarn",
        exec_cmd="yarn dlx",
        test_cmd="yarn test",
        build_cmd="yarn build",
        dev_cmd="yarn dev",
    ),
    "bun": PackageManagerConfig(
        name="bun",
        lock_file="bun.lockb",
        install_cmd="bun install",
        run_cmd="bun run",
        exec_cmd="bunx",
        test_cmd="bun test",
        build_cmd="bun run build",
        dev_cmd="bun run dev",
    ),
}

# 検出の優先順位
DETECTION_PRIORITY = ["pnpm", "bun", "yarn", "npm"]

# スクリプト/バイナリ名で安全な文字
SAFE_NAME_REGEX = re.compile(r"^[@a-zA-Z0-9_./-]+$")

# 引数で安全な文字
SAFE_ARGS_REGEX = re.compile(r"^[@a-zA-Z0-9\s_./:=,'\"*+-]+$")

PackageManagerName = Literal["npm", "pnpm", "yarn", "bun"]
DetectionSource = Literal[
    "environment",
    "project-config",
    "package.json",
    "lock-file",
    "global-config",
    "default",
    "none",
]


@dataclass
class PackageManagerResult:
    """パッケージマネージャー検出結果。"""

    name: str | None
    config: PackageManagerConfig | None
    source: DetectionSource


def get_config_path() -> Path:
    """グローバルなパッケージマネージャー設定パスを取得する。

    Returns:
        Path: Path オブジェクトを返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    return get_devgear_dir() / "package-manager.json"


def load_config() -> dict[str, Any] | None:
    """保存済みのパッケージマネージャー設定を読み込む。

    Returns:
        dict[str, Any] | None: 情報を格納した辞書を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    config_path = get_config_path()
    content = read_file(config_path)

    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return None
    return None


def save_config(config: dict[str, Any]) -> None:
    """パッケージマネージャー設定を保存する。

    Args:
        config: 設定

    Returns:
        None: 値を返しません。

    Raises:
        例外は発生しません。
    """
    config_path = get_config_path()
    write_file(config_path, json.dumps(config, indent=2))


def detect_from_lock_file(project_dir: str | Path | None = None) -> str | None:
    """プロジェクトディレクトリのロックファイルからパッケージマネージャーを検出する。

    Args:
        project_dir: プロジェクトディレクトリ

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    for pm_name in DETECTION_PRIORITY:
        pm = PACKAGE_MANAGERS[pm_name]
        lock_file_path = project_dir / pm.lock_file

        if lock_file_path.exists():
            return pm_name

    return None


def detect_from_package_json(project_dir: str | Path | None = None) -> str | None:
    """package.json の packageManager フィールドからパッケージマネージャーを検出する。

    Args:
        project_dir: プロジェクトディレクトリ

    Returns:
        str | None: str を返します。見つからない場合は None です。

    Raises:
        例外は発生しません。
    """
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    package_json_path = project_dir / "package.json"
    content = read_file(package_json_path)

    if content:
        try:
            pkg = json.loads(content)
            if pkg.get("packageManager"):
                # 形式: "pnpm@8.6.0" または単に "pnpm"
                pm_name = pkg["packageManager"].split("@")[0]
                if pm_name in PACKAGE_MANAGERS:
                    return pm_name
        except json.JSONDecodeError:
            pass

    return None


def get_available_package_managers() -> list[str]:
    """利用可能なパッケージマネージャー（システムにインストール済み）を取得する。

    警告: これは各パッケージマネージャーごとに子プロセスを起動する。
    セッション開始フック中には呼び出さないこと。
    高頻度経路では detect_from_lock_file() か detect_from_package_json() を使うこと。

    Returns:
        list[str]: str の一覧を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    available = []

    for pm_name in PACKAGE_MANAGERS:
        if command_exists(pm_name):
            available.append(pm_name)

    return available


def get_package_manager(
    *,
    project_dir: str | Path | None = None,
) -> PackageManagerResult:
    """現在のプロジェクトで使用するパッケージマネージャーを取得する。

    検出優先順位:
    1. 環境変数 CLAUDE_PACKAGE_MANAGER
    2. プロジェクト固有設定（.claude/package-manager.json）
    3. package.json の packageManager フィールド
    4. ロックファイルの検出
    5. グローバルなユーザー設定（~/.devgear/package-manager.json）
    6. 検出できない場合は name=None、source="none" を返す

    Args:
        project_dir: プロジェクトディレクトリ

    Returns:
        PackageManagerResult: 取得結果を返します。

    Raises:
        例外は発生しません。
    """
    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    # 1. 環境変数を確認する
    env_pm = os.environ.get("CLAUDE_PACKAGE_MANAGER")
    if env_pm and env_pm in PACKAGE_MANAGERS:
        return PackageManagerResult(
            name=env_pm,
            config=PACKAGE_MANAGERS[env_pm],
            source="environment",
        )

    # 2. プロジェクト固有設定を確認する
    project_config_path = project_dir / ".claude" / "package-manager.json"
    project_config_content = read_file(project_config_path)
    if project_config_content:
        try:
            config = json.loads(project_config_content)
            pm_name = config.get("packageManager")
            if pm_name and pm_name in PACKAGE_MANAGERS:
                return PackageManagerResult(
                    name=pm_name,
                    config=PACKAGE_MANAGERS[pm_name],
                    source="project-config",
                )
        except json.JSONDecodeError:
            pass

    # 3. package.json の packageManager フィールドを確認する
    from_package_json = detect_from_package_json(project_dir)
    if from_package_json:
        return PackageManagerResult(
            name=from_package_json,
            config=PACKAGE_MANAGERS[from_package_json],
            source="package.json",
        )

    # 4. ロックファイルを確認する
    from_lock_file = detect_from_lock_file(project_dir)
    if from_lock_file:
        return PackageManagerResult(
            name=from_lock_file,
            config=PACKAGE_MANAGERS[from_lock_file],
            source="lock-file",
        )

    # 5. グローバルなユーザー設定を確認する
    global_config = load_config()
    if global_config:
        pm_name = global_config.get("packageManager")
        if pm_name and pm_name in PACKAGE_MANAGERS:
            return PackageManagerResult(
                name=pm_name,
                config=PACKAGE_MANAGERS[pm_name],
                source="global-config",
            )

    # 6. 検出できなかった場合は None を返す（Node.js 以外のプロジェクトは PM 不要）
    return PackageManagerResult(
        name=None,
        config=None,
        source="none",
    )


def set_preferred_package_manager(pm_name: str) -> dict[str, Any]:
    """
    ユーザーの既定パッケージマネージャー（グローバル）を設定する。

    Args:
        pm_name: パッケージマネージャー名

    Returns:
        dict[str, Any]: 情報を格納した辞書を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    if pm_name not in PACKAGE_MANAGERS:
        raise ValueError(f"Unknown package manager: {pm_name}")

    config = load_config() or {}
    config["packageManager"] = pm_name
    config["setAt"] = __import__("datetime").datetime.now().isoformat()

    save_config(config)
    return config


def set_project_package_manager(
    pm_name: str,
    project_dir: str | Path | None = None,
) -> dict[str, Any]:
    """
    プロジェクトの既定パッケージマネージャーを設定する。

    Args:
        pm_name: パッケージマネージャー名
        project_dir: プロジェクトディレクトリ

    Returns:
        dict[str, Any]: 情報を格納した辞書を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    if pm_name not in PACKAGE_MANAGERS:
        raise ValueError(f"Unknown package manager: {pm_name}")

    if project_dir is None:
        project_dir = Path.cwd()
    else:
        project_dir = Path(project_dir)

    config_path = project_dir / ".claude" / "package-manager.json"

    config = {
        "packageManager": pm_name,
        "setAt": __import__("datetime").datetime.now().isoformat(),
    }

    write_file(config_path, json.dumps(config, indent=2))
    return config


def get_run_command(
    script: str,
    *,
    project_dir: str | Path | None = None,
) -> str | None:
    """
    スクリプトを実行するコマンドを取得する。

    PM が検出できない場合（Node.js 以外のプロジェクト等）は None を返す。

    Args:
        script: スクリプト名
        project_dir: プロジェクトディレクトリ

    Returns:
        コマンド文字列。PM が未検出の場合は None を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    if not script or not isinstance(script, str):
        raise ValueError("Script name must be a non-empty string")
    if not SAFE_NAME_REGEX.match(script):
        raise ValueError(f"Script name contains unsafe characters: {script}")

    pm = get_package_manager(project_dir=project_dir)

    # PM が検出されなかった場合（Node.js 非依存プロジェクト）
    if pm.config is None:
        return None

    if script == "install":
        return pm.config.install_cmd
    elif script == "test":
        return pm.config.test_cmd
    elif script == "build":
        return pm.config.build_cmd
    elif script == "dev":
        return pm.config.dev_cmd
    else:
        return f"{pm.config.run_cmd} {script}"


def get_exec_command(
    binary: str,
    args: str = "",
    *,
    project_dir: str | Path | None = None,
) -> str | None:
    """
    パッケージバイナリを実行するコマンドを取得する。

    PM が検出できない場合（Node.js 以外のプロジェクト等）は None を返す。

    Args:
        binary: バイナリ名
        args: 引数文字列
        project_dir: プロジェクトディレクトリ

    Returns:
        コマンド文字列。PM が未検出の場合は None を返します。

    Raises:
        ValueError: 入力の不正や処理失敗時に発生します。
    """
    if not binary or not isinstance(binary, str):
        raise ValueError("Binary name must be a non-empty string")
    if not SAFE_NAME_REGEX.match(binary):
        raise ValueError(f"Binary name contains unsafe characters: {binary}")
    if args and isinstance(args, str) and not SAFE_ARGS_REGEX.match(args):
        raise ValueError(f"Arguments contain unsafe characters: {args}")

    pm = get_package_manager(project_dir=project_dir)

    # PM が検出されなかった場合（Node.js 非依存プロジェクト）
    if pm.config is None:
        return None

    return f"{pm.config.exec_cmd} {binary}{' ' + args if args else ''}"


def get_selection_prompt() -> str:
    """Node.js パッケージマネージャーが未設定の場合に設定方法を返す。

    Node.js プロジェクトで PM が検出できなかった場合のガイダンス文字列を返す。
    Node.js 以外のプロジェクトでは PM は不要なため、その旨を確認してから呼び出すこと。

    Returns:
        str: 設定方法を示す文字列を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    message = "[PackageManager] Node.js project detected but no package manager preference found.\n"
    message += "Supported package managers: " + ", ".join(PACKAGE_MANAGERS.keys()) + "\n"
    message += "\nTo set your preferred package manager:\n"
    message += "  - Global: Set CLAUDE_PACKAGE_MANAGER environment variable\n"
    message += '  - Or add to ~/.devgear/package-manager.json: {"packageManager": "pnpm"}\n'
    message += '  - Or add to package.json: {"packageManager": "pnpm@8"}\n'
    message += "  - Or add a lock file to your project (e.g., pnpm-lock.yaml)\n"

    return message


def get_command_pattern(action: str) -> str:
    """すべてのパッケージマネージャーのコマンドに一致する正規表現パターンを生成する。

    Args:
        action: action の値

    Returns:
        str: 文字列を返します。

    Raises:
        例外は発生しません。
    """
    patterns: list[str] = []
    trimmed_action = action.strip()

    if trimmed_action == "dev":
        patterns = [
            "npm run dev",
            "pnpm( run)? dev",
            "yarn dev",
            "bun run dev",
        ]
    elif trimmed_action == "install":
        patterns = [
            "npm install",
            "pnpm install",
            "yarn( install)?",
            "bun install",
        ]
    elif trimmed_action == "test":
        patterns = [
            "npm test",
            "pnpm test",
            "yarn test",
            "bun test",
        ]
    elif trimmed_action == "build":
        patterns = [
            "npm run build",
            "pnpm( run)? build",
            "yarn build",
            "bun run build",
        ]
    else:
        # 汎用 run コマンド - 正規表現メタ文字をエスケープする
        escaped = re.escape(trimmed_action)
        patterns = [
            f"npm run {escaped}",
            f"pnpm( run)? {escaped}",
            f"yarn {escaped}",
            f"bun run {escaped}",
        ]

    return f"({' | '.join(patterns).replace(' | ', '|')})"


__all__ = [
    "DETECTION_PRIORITY",
    "PACKAGE_MANAGERS",
    "PackageManagerConfig",
    "PackageManagerResult",
    "detect_from_lock_file",
    "detect_from_package_json",
    "get_available_package_managers",
    "get_command_pattern",
    "get_exec_command",
    "get_package_manager",
    "get_run_command",
    "get_selection_prompt",
    "set_preferred_package_manager",
    "set_project_package_manager",
]
