#!/usr/bin/env python3
"""フラグに応じてフックの有効・無効を切り替えるランチャーです。

フック設定を見て、必要な場合だけターゲットスクリプトを実行します。
追加引数はターゲットへそのまま渡します。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    SESSION_START_HOOK_IDS,
    emit_session_start_output,
    write_stderr,
    write_stdout,
)
from bluecore.lib.hook_flags import is_hook_enabled

REPO_ROOT = Path(__file__).resolve().parents[3]

# 入力切り捨て時に config-protection をバイパスさせないためのガード対象 hook id 集合。
# 設定ファイル保護は truncated payload を見逃すとバイパスに悪用されうるため、
# run_with_flags 側でブロックする。
_TRUNCATION_GUARD_HOOK_IDS = frozenset({"pre:config-protection"})


def read_raw_stdin_with_truncation(max_bytes: int = MAX_STDIN_BYTES) -> tuple[str, bool]:
    """標準入力を読み取り、切り捨ての有無を返します。

    Args:
        max_bytes: 読み取る最大バイト数です。

    Returns:
        読み取った文字列と、切り捨てが発生したかどうかのタプルを返します。

    Raises:
        例外は発生しません。
    """
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is not None:
        raw_bytes = stdin_buffer.read(max_bytes + 1)
    else:
        # io.StringIO など .buffer を持たない stdin を想定したフォールバック。
        # バイト上限を大きく超える無制限 read を避けるため、最大 +1 文字だけ読む。
        raw_text = sys.stdin.read(max_bytes + 1)
        raw_bytes = raw_text.encode("utf-8", errors="replace")
    truncated = len(raw_bytes) > max_bytes
    if truncated:
        raw_bytes = raw_bytes[:max_bytes]
    return raw_bytes.decode("utf-8", errors="replace"), truncated


def build_env() -> dict[str, str]:
    """サブプロセス用の環境変数を構築します。

    Returns:
        実行用に調整した環境変数の辞書を返します。

    Raises:
        例外は発生しません。
    """
    env = os.environ.copy()
    env["CLAUDE_PLUGIN_ROOT"] = str(REPO_ROOT)

    pythonpath = env.get("PYTHONPATH")
    paths = [str(REPO_ROOT / "src")]
    if pythonpath:
        paths.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def _truncation_blocked_message(hook_id: str, max_bytes: int) -> str:
    """入力切り捨て時のブロック理由メッセージを生成する。

    Args:
        hook_id: 対象フック ID。
        max_bytes: 入力の最大バイト数。

    Returns:
        stderr に書き出すメッセージ。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Hook input exceeded {max_bytes} bytes for {hook_id}. "
        "Refusing to bypass protection on a truncated payload. "
        "Retry with a smaller edit."
    )


def _command_for_existing_file(candidate: Path, args: list[str]) -> list[str] | None:
    """既存ファイルのパスからコマンドリストを返す。

    拡張子と実行可能ビットに基づいてインタープリタを選択します。
    対応する拡張子でも実行可能でもない場合は None を返します。

    Args:
        candidate: 実在するファイルへの Path オブジェクトです。
        args: コマンドに追加する引数リストです。

    Returns:
        subprocess に渡すコマンドリスト、または判定不能な場合 None。

    Raises:
        例外は発生しません。
    """
    suffix = candidate.suffix.lower()
    if suffix == ".py":
        return [sys.executable, str(candidate), *args]
    if suffix in {".sh", ".bash"}:
        return ["bash", str(candidate), *args]
    if os.name == "nt" and suffix in {".cmd", ".bat"}:
        return ["cmd", "/c", str(candidate), *args]
    if os.access(candidate, os.X_OK):
        return [str(candidate), *args]
    return None


def resolve_target_command(
    target: str,
    args: list[str] | None = None,
    *,
    plugin_root: Path | None = None,
) -> list[str]:
    """ターゲット指定から実行コマンドを解決します。

    Args:
        target: ターゲットのパスまたはモジュール名です。
        args: ターゲットへ渡す追加引数です。

    Returns:
        subprocess に渡すコマンドリストを返します。

    Raises:
        例外は発生しません。
    """
    args = list(args or [])
    plugin_root = plugin_root or REPO_ROOT
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = candidate if candidate.exists() else plugin_root / candidate
        try:
            if candidate.resolve().is_relative_to(plugin_root) and candidate.exists():
                cmd = _command_for_existing_file(candidate, args)
                if cmd is not None:
                    return cmd
        except OSError:
            pass
    elif candidate.exists():
        cmd = _command_for_existing_file(candidate, args)
        if cmd is not None:
            return cmd

    return [sys.executable, "-m", target, *args]


def _drain_stdin() -> None:
    """フック無効時に stdin を読み捨てる。"""
    stdin_buffer = getattr(sys.stdin, "buffer", None)
    if stdin_buffer is not None:
        stdin_buffer.read()
    else:
        sys.stdin.read()


def _run_target(hook_id: str, target: str, target_args: list[str], raw: str) -> int:
    """ターゲットをサブプロセスで実行し、stdout/stderr を転送して終了コードを返す。

    Args:
        hook_id: フック ID（SESSION_START_HOOK_IDS 判定に使用）。
        target: ターゲットのパスまたはモジュール名。
        target_args: ターゲットへ渡す追加引数。
        raw: 子プロセスへ渡す stdin。

    Returns:
        子プロセスの終了コード。OSError 発生時は 1。
    """
    try:
        result = subprocess.run(
            resolve_target_command(target, target_args, plugin_root=REPO_ROOT),
            input=raw,
            text=True,
            capture_output=True,
            env=build_env(),
        )
    except OSError as err:
        write_stderr(f"[Hook] Error running {hook_id}: {err}\n")
        return 1

    if result.stdout:
        write_stdout(result.stdout)
    elif hook_id in SESSION_START_HOOK_IDS:
        write_stdout(emit_session_start_output())

    if result.stderr:
        write_stderr(result.stderr)

    if hook_id in SESSION_START_HOOK_IDS and result.returncode != 0:
        return 0
    return result.returncode


def main() -> int:
    """フックランチャーのメイン処理を実行します。

    Returns:
        ターゲットの終了コード、またはエラー時の 1 を返します。

    Args:
        引数はありません。

    Raises:
        例外は発生しません。
    """
    if len(sys.argv) < 3:
        write_stderr("run_with_flags: 引数が不足しています (hook_id target が必要)\n")
        return 1

    hook_id = sys.argv[1]
    target = sys.argv[2]
    profiles_csv = sys.argv[3] if len(sys.argv) > 3 else None
    target_args = sys.argv[4:] if len(sys.argv) > 4 else []

    if not is_hook_enabled(hook_id, profiles=profiles_csv):
        _drain_stdin()
        return 0

    raw, truncated = read_raw_stdin_with_truncation()
    if truncated and hook_id in _TRUNCATION_GUARD_HOOK_IDS:
        write_stderr(_truncation_blocked_message(hook_id, MAX_STDIN_BYTES) + "\n")
        return 2

    return _run_target(hook_id, target, target_args, raw)


if __name__ == "__main__":
    sys.exit(main())
