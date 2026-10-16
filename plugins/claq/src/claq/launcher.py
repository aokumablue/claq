#!/usr/bin/env python3
"""claq フックのインプロセスランチャー。

Claude Code 等のハーネスから `python3 launcher.py [--bg] <module> [args...]`
の形で起動される。PATH 上の `python3` をそのまま使い、対象モジュールを
サブプロセスを spawn せず runpy でインプロセス実行する。Python 3.12 未満
では hook を実行せず stderr に理由を書いて 0 で終了する（fail-open）。
venv への自己置換は行わない。stdin はターゲット自身が
`hook_common.read_raw_stdin()` 等で直接読む（launcher は代読しない）。

Python 3.12 未満でも構文として読めるよう、本ファイルでは新しい構文を使わない。
"""

from __future__ import annotations

import json
import os
import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
_USAGE = "Usage: python3 src/claq/launcher.py [--bg] <module> [args...]"


def build_env() -> dict[str, str]:
    """detach する子プロセス用の環境変数を構築します。

    repo-local `.venv` の有無は見ず、VIRTUAL_ENV の付与や venv/bin の PATH 前置も行わない。

    Returns:
        CLAUDE_PLUGIN_ROOT（未設定時のみ REPO_ROOT）、PYTHONIOENCODING=utf-8:replace、
        REPO_ROOT/src を先頭に置いた PYTHONPATH を含む環境変数の辞書。
    """
    env = os.environ.copy()
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT))
    # detach した子は `-m <module>` で起動するため `main()` を通らず、
    # `force_utf8_streams()` が効かない。子の警告は日本語を含み bg ログへ
    # リダイレクトされるため、非 UTF-8 ロケールでは UnicodeEncodeError が bg ログの
    # 中だけで起きる。子にも UTF-8 固定を効かせる。errors を ``strict`` にすると
    # surrogateescape 由来のサロゲートで子だけが失敗する。``PYTHONIOENCODING`` は
    # stdout/stderr を分けられないため、子の stderr は親と異なり ``replace`` になるが、
    # 子の出力は bg ログへ落ちる診断であり「読める形で残る」ことを優先する。
    env["PYTHONIOENCODING"] = "utf-8:replace"

    pythonpath = env.get("PYTHONPATH")
    paths = [str(REPO_ROOT / "src")]
    if pythonpath:
        paths.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def force_utf8_streams() -> None:
    """stdout / stderr を UTF-8 へ固定します。

    フックの出力は日本語を含む。stdout がパイプで UTF-8 モードが無効だと、Python は
    ロケール由来のエンコーディング（Windows の cp932、``LC_ALL=C`` の Linux の ASCII）を
    使い、非 ASCII を書いた時点で ``UnicodeEncodeError`` になってフックは注入も deny も
    できないまま exit 1 で落ちる。ホスト（Node 系 CLI）はフックのパイプを UTF-8 として
    読むため、3 OS 共通で UTF-8 に固定する。``reconfigure`` を持たないストリーム
    （テストの差し替え等）はそのままにする。
    """
    # stderr の errors は Python 既定の ``backslashreplace`` を保つ。``replace``
    # にすると、surrogateescape で読まれた不正 UTF-8 のファイル名が ``\udcXX``
    # から ``?`` に落ち、診断に必要な情報が消える（stdout は host が読む
    # データなので ``replace`` でよい）。
    for stream, errors in ((sys.stdout, "replace"), (sys.stderr, "backslashreplace")):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (OSError, ValueError):
            pass


def _unsupported_python_exit_code() -> int | None:
    """Python 3.12 未満なら fail-open の終了コード 0 を返します。

    fail-open にするのは、exit 2 だと全 Edit/Bash が拒否されセッションが即死し、守るべき
    対象より被害が大きいため。ただし保護 hook が静かに無効化されている事実を見落とさせない
    よう、人間可読の行に加えて grep 可能な構造化 JSON も stderr へ書く（claq の他モジュールは
    まだ import できず、stdout は host ごとに hook 種別で契約が異なるため触らない）。

    Returns:
        3.12 以上なら None。未満なら stderr に理由を書いて 0。
    """
    # PATH 上の python3 はパッケージ requires-python より古いことがある。
    if sys.version_info >= (3, 12):  # noqa: UP036
        return None
    version = ".".join(str(part) for part in sys.version_info[:3])
    sys.stderr.write(
        f"ERROR: claq requires Python 3.12+; `python3` is {version}. "
        "Point `python3` on PATH at 3.12+ (claq does not create a venv).\n"
    )
    sys.stderr.write(
        json.dumps(
            {
                "claqProtectionDisabled": True,
                "reason": "unsupported_python_version",
                "detectedVersion": version,
                "requiredVersion": "3.12+",
            }
        )
        + "\n"
    )
    return 0


def _run_module_in_process(target: str, target_args: list[str]) -> int:
    """モジュールをインプロセスで実行し、終了コードを返します。

    ``run_name="__main__"`` で対象モジュールの `if __name__ == "__main__":` 経路を
    サブプロセス実行と同一の入口から通す。mem.cli 等はサブコマンドを ``sys.argv[1]``
    から読むため、先に ``sys.argv`` を差し替える。全フックは SystemExit 経由で終了する。

    Returns:
        ターゲットの終了コード。例外は内部で捕捉し 1 を返す。
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
        # exit 1 は PreToolUse の契約で「non-blocking error = ツールは実行される」。
        # つまり保護フックの deny が黙って allow へ反転する。exit 2 へ倒さないのは、
        # 全入力で raise するバグを踏んだとき Bash ごと塞がって復旧手段を失うため
        # （`launcher.py` 冒頭の fail-open と同じ理由）。代わりに、他の無効化経路と
        # 同じ `claqProtectionDisabled` マーカーを出して**保護喪失を可視化**する。
        sys.stderr.write(f"ERROR: {target}: {exc}\n")
        sys.stderr.write(
            '{"claqProtectionDisabled": true, "reason": "hook_raised", '
            f'"module": "{target}"}}\n'
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    """ランチャーのメインエントリポイントです。

    Returns:
        ターゲットの終了コード、またはエラー時は 1。Python 3.12 未満では hook_common /
        harness を import せず 0 を返す（fail-open）。
    """
    force_utf8_streams()

    unsupported = _unsupported_python_exit_code()
    if unsupported is not None:
        return unsupported

    src_dir = str(REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from claq.hooks.detach import detach_process
    from claq.hooks.hook_common import read_raw_stdin, write_stderr
    from claq.lib.env_pointer import write_env_pointer

    # md（agents/commands/skills）がベンダ固有パスを書かずに plugin root を
    # 解決できるよう、全 hook 起動のたびに ~/.claq/env.sh ポインタを更新する。
    # 失敗しても hook 本来の処理は妨げない。
    write_env_pointer(REPO_ROOT)

    args = list(sys.argv[1:] if argv is None else argv)
    background = bool(args) and args[0] == "--bg"
    if background:
        args = args[1:]
    if not args:
        print(_USAGE, file=sys.stderr)
        return 1

    target, target_args = args[0], args[1:]
    if not background:
        return _run_module_in_process(target, target_args)

    # --bg は host に関わらず常に detach し、呼び出し側フックを同期ブロックしない。
    # hooks.json / claq-helpers.sh のターゲットはすべて dotted module name なので `-m` で起動する。
    if detach_process([sys.executable, "-m", target, *target_args], read_raw_stdin(), env=build_env()):
        return 0
    # docs/adr/hook-failure-direction.md: 親の exit code は「子の起動を受け付けたか」を表す。
    # 受付そのものに失敗した以上、成功を返すと呼び出し側は起動されていない処理を
    # 受付成功と誤認する（子の処理結果は依然として非同期）。
    write_stderr(f"[Hook] Error detaching {target}\n")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
