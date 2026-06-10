#!/usr/bin/env python3
"""リポジトリ内の Python モジュールと実行可能スクリプトの汎用ランチャー。"""

from __future__ import annotations

import math
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# hooks.json の最長エントリ（600 秒）より先に自決して孫プロセスの孤立を防ぐ。
# それより短い timeout のエントリでは Claude Code 側の kill が先に働く。
DEFAULT_SUBPROCESS_TIMEOUT = 590.0


def _subprocess_timeout() -> float:
    """サブプロセスの timeout 秒数を環境変数から解決します。

    Args:
        なし

    Returns:
        BLUECORE_HOOK_TIMEOUT が正の有限数値ならその秒数、未設定・無効値なら既定の 590 秒。

    Raises:
        例外は発生しません。
    """
    raw = os.environ.get("BLUECORE_HOOK_TIMEOUT")
    if raw:
        try:
            value = float(raw)
        except ValueError:
            return DEFAULT_SUBPROCESS_TIMEOUT
        if value > 0 and math.isfinite(value):
            return value
    return DEFAULT_SUBPROCESS_TIMEOUT


def _runtime_python() -> tuple[str, Path | None]:
    """実行に使う Python を解決します。"""
    for candidate in (
        REPO_ROOT / ".venv" / "bin" / "python3",
        REPO_ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate), candidate.parent.parent

    return sys.executable, None


def build_env() -> dict[str, str]:
    """サブプロセス用の環境変数を構築します。

    Args:
        なし

    Returns:
        PYTHONPATH、プラグインルート、必要なら repo-local venv の PATH が
        設定された環境変数の辞書を返します。

    Raises:
        例外は発生しません。
    """
    env = os.environ.copy()
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT))

    pythonpath = env.get("PYTHONPATH")
    paths = [str(REPO_ROOT / "src")]
    if pythonpath:
        paths.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)

    _, venv_root = _runtime_python()
    if venv_root is not None:
        venv_bin = str(venv_root / "bin")
        path = env.get("PATH")
        paths = [venv_bin]
        if path:
            paths.append(path)
        env["PATH"] = os.pathsep.join(paths)
        env["VIRTUAL_ENV"] = str(venv_root)

    return env


def resolve_command(target: str, args: list[str]) -> list[str]:
    """ターゲットから実行コマンドを解決します。

    Args:
        target: 実行するモジュール名またはスクリプトパスです。
        args: ターゲットに渡す引数のリストです。

    Returns:
        subprocess に渡すコマンドとその引数のリストを返します。

    Raises:
        例外は発生しません。
    """
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = candidate if candidate.exists() else REPO_ROOT / candidate

    if candidate.exists():
        suffix = candidate.suffix.lower()
        if suffix == ".py":
            runtime_python, _ = _runtime_python()
            return [runtime_python, str(candidate), *args]
        if suffix in {".sh", ".bash"}:
            return ["bash", str(candidate), *args]
        if os.name == "nt" and suffix in {".cmd", ".bat"}:
            return ["cmd", "/c", str(candidate), *args]
        if os.access(candidate, os.X_OK):
            return [str(candidate), *args]

    runtime_python, _ = _runtime_python()
    return [runtime_python, "-m", target, *args]


def main(argv: list[str] | None = None) -> int:
    """ランチャーのメインエントリポイントです。

    Args:
        argv: コマンドライン引数のリストです。

    Returns:
        ターゲットの終了コード、またはエラー時は 1 を返します。

    Raises:
        例外はキャッチされ、エラーメッセージとして出力されます。
    """
    # PYTHONPATH 未設定時の自己解決（launcher.py はサブプロセスに PYTHONPATH を渡すが自身には未設定のため）
    src_dir = str(REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python3 src/bluecore/launcher.py <module-or-script> [args...]", file=sys.stderr)
        return 1

    target, target_args = args[0], args[1:]
    if sys.stdin.isatty():
        raw_input = ""
    else:
        raw_input = sys.stdin.read()

    try:
        result = subprocess.run(
            resolve_command(target, target_args),
            input=raw_input,
            text=True,
            capture_output=True,
            env=build_env(),
            timeout=_subprocess_timeout(),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    if result.stdout:
        sys.stdout.write(result.stdout)

    if result.stderr:
        sys.stderr.write(result.stderr)

    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
