#!/usr/bin/env python3
"""bluecore フックのインプロセスランチャー。

Claude Code 等のハーネスから `python3 launcher.py [--bg] <module> [args...]`
の形で起動される。repo-local venv が見つかれば os.execve で自己置換した
うえで、対象モジュールをサブプロセスを spawn せず runpy でインプロセス
実行する。stdin はターゲット自身が `hook_common.read_raw_stdin()` 等で
直接読む（launcher は代読しない）。
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_LAUNCHER_PATH = str(REPO_ROOT / "src" / "bluecore" / "launcher.py")


def _runtime_python() -> tuple[str, Path | None]:
    """実行に使う repo-local venv の Python を解決します。

    Args:
        なし

    Returns:
        (python 実行ファイルのパス文字列, venv ルート) のタプル。
        repo-local venv が見つからなければ (sys.executable, None) を返す。

    Raises:
        例外は発生しません。
    """
    for candidate in (
        REPO_ROOT / ".venv" / "bin" / "python3",
        REPO_ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate), candidate.parent.parent

    return sys.executable, None


def build_env() -> dict[str, str]:
    """子プロセス/execve 用の環境変数を構築します。

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


def _reexec_into_venv_if_needed() -> None:
    """repo-local venv の Python へ os.execve で自己置換します。

    venv が見つからない場合（初回インストール前）はシステム Python の
    まま続行します（fail-open）。os.execve はプロセスイメージを置換する
    だけで fork しないため、成功時にプロセス数は増えません。

    venv の python3 実行ファイルは多くの場合ベースインタプリタへの
    symlink（コピーではない）であり、``os.path.samefile(sys.executable,
    venv_python)`` は symlink 先の実体が同じというだけで True になって
    しまう。Python の venv 有効化は「起動に使われたパス」に隣接する
    pyvenv.cfg の有無で決まる（``sys.prefix``）ため、実体比較ではなく
    ``sys.prefix != sys.base_prefix``（何らかの venv が有効か）で判定し、
    有効な venv がある場合のみ、その prefix が対象 venv と一致するかを
    実体比較で確認する。system python 実行時（多くのフック起動はこちら）
    は必ず exec して venv の site-packages（pydantic 等）を有効化する。

    Args:
        なし

    Returns:
        None（execve に成功すると戻らない。venv 不在・既に対象 venv 上・
        execve 失敗時のみ戻る）

    Raises:
        例外は発生しません。
    """
    venv_python, venv_root = _runtime_python()
    if venv_root is None:
        return

    if sys.prefix != sys.base_prefix:
        # 既に何らかの venv が有効。対象 venv と一致するなら re-exec 不要。
        try:
            if os.path.samefile(sys.prefix, venv_root):
                return
        except OSError:
            pass

    try:
        os.execve(venv_python, [venv_python, _LAUNCHER_PATH, *sys.argv[1:]], build_env())
    except OSError:
        # exec 失敗時は現行インタプリタで続行する（fail-open）。
        return


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

    Args:
        argv: コマンドライン引数のリストです。

    Returns:
        ターゲットの終了コード、またはエラー時は 1 を返します。

    Raises:
        例外は発生しません。
    """
    _reexec_into_venv_if_needed()

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
