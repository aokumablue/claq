#!/usr/bin/env python3
"""claq フックのインプロセスランチャー。

Claude Code 等のハーネスから `python3 launcher.py [--bg] <module> [args...]`
の形で起動される。PATH 上の `python3` をそのまま使い、対象モジュールを
サブプロセスを spawn せず runpy でインプロセス実行する。Python 3.12 未満
では hook を実行せず stderr に理由を書いて 0 で終了する（fail-open）。
venv への自己置換は行わない。stdin はターゲット自身が
`hook_common.read_raw_stdin()` 等で直接読む（launcher は代読しない）。
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
    """子プロセス用の環境変数を構築します。

    repo-local `.venv` の有無は見ない。VIRTUAL_ENV の付与や venv/bin の
    PATH 前置は行わない。

    Args:
        なし

    Returns:
        CLAUDE_PLUGIN_ROOT（未設定時のみ REPO_ROOT）、PYTHONIOENCODING=utf-8:replace、
        REPO_ROOT/src を先頭に置いた PYTHONPATH を含む環境変数の辞書。

    Raises:
        例外は発生しません。
    """
    env = os.environ.copy()
    env.setdefault("CLAUDE_PLUGIN_ROOT", str(REPO_ROOT))
    # detach した子は `-m <module>` で起動するため `main()` を通らず、
    # `force_utf8_streams()` が効かない。子の警告（`handoff 失敗: ...` 等）は
    # 日本語を含み、bg ログへリダイレクトされるため、非 UTF-8 ロケールでは
    # UnicodeEncodeError が bg ログの中だけで起き、次セッションの通知に
    # 化けた 1 行として現れる。子にも同じ UTF-8 固定を効かせる。
    # errors を明示する。既定の ``strict`` だと、surrogateescape 由来の
    # サロゲートを含む文字列で子だけが ``UnicodeEncodeError`` になる。
    # ``PYTHONIOENCODING`` は stdout/stderr を分けられないため、子の stderr は
    # 親（`force_utf8_streams` が ``backslashreplace`` を保つ）と異なり
    # ``replace`` になる。子の出力は bg ログへ落ちる診断であり、
    # 「読める形で残る」ことを優先する。
    env["PYTHONIOENCODING"] = "utf-8:replace"

    pythonpath = env.get("PYTHONPATH")
    paths = [str(REPO_ROOT / "src")]
    if pythonpath:
        paths.append(pythonpath)
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def force_utf8_streams() -> None:
    """stdout / stderr を UTF-8 へ固定します。

    フックの出力（SessionStart の注入コンテキスト、deny 理由、警告）は日本語を
    含む。Python は stdout がパイプのとき、UTF-8 モードが無効ならロケール由来の
    エンコーディングを使う — Windows の既定コードページ（日本語環境なら cp932）
    や、``LC_ALL=C`` の Linux では ASCII になる。その状態で非 ASCII を書くと
    ``UnicodeEncodeError`` になり、フックは注入も deny もできないまま exit 1 で
    落ちる（実測: ``PYTHONUTF8=0 LC_ALL=C`` で ``mem.cli context`` が
    ``'ascii' codec can't encode characters`` で失敗）。

    ホスト（Node 系 CLI）はフックのパイプを UTF-8 として読むため、UTF-8 固定が
    正しい出力である。プラットフォーム分岐ではなく、3 OS 共通で同じ 1 本の
    処理として行う。``reconfigure`` を持たないストリーム（テストの差し替え等）は
    そのままにする。

    Args:
        なし

    Returns:
        なし

    Raises:
        例外は発生しません。
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

    fail-open 自体は維持する（exit 2 にすると全 Edit/Bash が拒否され
    セッションが即死し、守るべき対象より被害が大きいため）。ただし
    「保護 hook が静かに無効化されている」事実を見落とさせないよう、
    人間可読の行に加えて grep 可能な構造化 JSON も stderr へ書く
    （claq 自体のモジュールは Python 3.12+ 前提の構文を使いうるため、
    この時点ではまだ import できず、stdout は host ごとに hook 種別で
    契約が異なるため触らない。stderr のみで完結させる）。

    Args:
        なし

    Returns:
        3.12 以上なら None。未満なら stderr に理由を書いて 0。

    Raises:
        例外は発生しません。
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


def _resolve_module_command(target: str, target_args: list[str]) -> list[str]:
    """detach（--bg 起動）用のコマンドリストを構築します。

    hooks.json / claq-helpers.sh のターゲットはすべて dotted module
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
    force_utf8_streams()

    unsupported = _unsupported_python_exit_code()
    if unsupported is not None:
        return unsupported

    src_dir = str(REPO_ROOT / "src")
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from claq.hooks.hook_common import detach_process, read_raw_stdin, write_stderr
    from claq.lib.env_pointer import write_env_pointer

    # md（agents/commands/skills）がベンダ固有パスを書かずに plugin root を
    # 解決できるよう、全 hook 起動のたびに ~/.claq/env.sh ポインタを更新する
    # （M-02 対応）。失敗しても hook 本来の処理は妨げない。
    write_env_pointer(REPO_ROOT)

    args = list(sys.argv[1:] if argv is None else argv)
    background = bool(args) and args[0] == "--bg"
    if background:
        args = args[1:]
    if not args:
        print(_USAGE, file=sys.stderr)
        return 1

    target, target_args = args[0], args[1:]

    if background:
        # --bg は host に関わらず常に detach する（同一処理を host 非依存で
        # 実現するため）。呼び出し側フックを同期ブロックしない。
        raw = read_raw_stdin()
        launched = detach_process(_resolve_module_command(target, target_args), raw, env=build_env())
        if not launched:
            # docs/adr/hook-failure-direction.md: 親の exit code は「子の起動を受け付けたか」を表す。
            # 受付そのものに失敗した以上、成功を返すと呼び出し側は起動されて
            # いない処理を受付成功と誤認する（子の処理結果は依然として非同期）。
            write_stderr(f"[Hook] Error detaching {target}\n")
            return 1
        return 0

    return _run_module_in_process(target, target_args)


if __name__ == "__main__":
    raise SystemExit(main())
