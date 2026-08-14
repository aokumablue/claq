#!/usr/bin/env python3
"""bluecore フックのインプロセスランチャー。

Claude Code 等のハーネスから `python3 launcher.py [--bg] <module> [args...]`
の形で起動される。PATH 上の `python3` をそのまま使い、対象モジュールを
サブプロセスを spawn せず runpy でインプロセス実行する。Python 3.12 未満
では hook を実行せず stderr に理由を書いて 0 で終了する（fail-open）。
venv への自己置換は行わない。stdin はターゲット自身が
`hook_common.read_raw_stdin()` 等で直接読む（launcher は代読しない）。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def build_env() -> dict[str, str]:
    """子プロセス用の環境変数を構築します。

    repo-local `.venv` の有無は見ない。VIRTUAL_ENV の付与や venv/bin の
    PATH 前置は行わない。

    Args:
        なし

    Returns:
        CLAUDE_PLUGIN_ROOT（未設定時のみ REPO_ROOT）と、REPO_ROOT/src を
        先頭に置いた PYTHONPATH を含む環境変数の辞書。

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
    return env


def _unsupported_python_exit_code() -> int | None:
    """Python 3.12 未満なら fail-open の終了コード 0 を返します。

    Args:
        なし

    Returns:
        3.12 以上なら None。未満なら stderr に 1 行理由を書いて 0。

    Raises:
        例外は発生しません。
    """
    # PATH 上の python3 はパッケージ requires-python より古いことがある。
    if sys.version_info >= (3, 12):  # noqa: UP036
        return None
    version = ".".join(str(part) for part in sys.version_info[:3])
    sys.stderr.write(
        f"ERROR: bluecore requires Python 3.12+; `python3` is {version}. "
        "Point `python3` on PATH at 3.12+ (bluecore does not create a venv).\n"
    )
    return 0


def _run_module_in_process(target: str, target_args: list[str]) -> int:
    """モジュールをインプロセスで実行し、終了コードを返します。

    runpy.run_module に run_name="__main__" を渡すことで、対象モジュールの
    `if __name__ == "__main__":` 経路を従来のサブプロセス実行と同一の
    入口から通す。sys.argv[1:] にターゲット引数を設定してから実行する
    （mem.cli 等はサブコマンドを sys.argv[1] から読むため）。

    Args:
        target: 実行するモジュールの dotted name。
        target_args: ターゲットへ渡す追加引数。

    Returns:
        ターゲットの終了コード。全フックは SystemExit 経由で終了する
        （raise SystemExit(main()) / sys.exit(main()) 形式）ことを確認済み。

    Raises:
        例外は発生しません（内部で捕捉し 1 を返す）。
    """
    sys.argv = [target, *target_args]
    try:
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        sys.stderr.write(str(exc.code) + "\n")
        return 1
    except Exception as exc:  # noqa: BLE001 - フックは常に終了コードを返す契約にする
        sys.stderr.write(f"ERROR: {target}: {exc}\n")
        return 1
    return 0


def _resolve_module_command(target: str, target_args: list[str]) -> list[str]:
    """detach（--bg かつ非 Claude ハーネス）起動用のコマンドリストを構築します。

    hooks.json / bluecore-helpers.sh のターゲットはすべて dotted module
    name であることを確認済みのため、`-m` 起動のみをサポートする。

    Args:
        target: 実行するモジュールの dotted name。
        target_args: ターゲットへ渡す追加引数。

    Returns:
        subprocess に渡すコマンドリスト。

    Raises:
        例外は発生しません。
    """
    return [sys.executable, "-m", target, *target_args]


def main(argv: list[str] | None = None) -> int:
    """ランチャーのメインエントリポイントです。

    Python 3.12 未満では hook_common / harness を import せず 0 を返します。

    Args:
        argv: コマンドライン引数のリストです。

    Returns:
        ターゲットの終了コード、またはエラー時は 1 を返します。
        Python 3.12 未満では 0 を返します（fail-open）。

    Raises:
        例外は発生しません。
    """
    unsupported = _unsupported_python_exit_code()
    if unsupported is not None:
        return unsupported

    src_dir = str(REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from bluecore.hooks.hook_common import detach_process, read_raw_stdin, write_stderr
    from bluecore.lib.harness import detect_harness

    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python3 src/bluecore/launcher.py [--bg] <module> [args...]", file=sys.stderr)
        return 1

    background = False
    if args[0] == "--bg":
        background = True
        args = args[1:]

    if not args:
        print("Usage: python3 src/bluecore/launcher.py [--bg] <module> [args...]", file=sys.stderr)
        return 1

    target, target_args = args[0], args[1:]

    if background and detect_harness() != "claude":
        # Claude Code はホスト側で非同期実行するためインプロセス実行のまま
        # 進めてよい。Codex 等 async 未サポートのハーネスでは detach して
        # 即 0 を返す（フックがセッションを同期ブロックしないようにする）。
        raw = read_raw_stdin()
        launched = detach_process(_resolve_module_command(target, target_args), raw, env=build_env())
        if not launched:
            write_stderr(f"[Hook] Error detaching {target}\n")
        return 0

    return _run_module_in_process(target, target_args)


if __name__ == "__main__":
    raise SystemExit(main())
