"""runtime/ple4-hook（および Windows 版 .cmd）の起動契約テスト。

hooks.json の全エントリはこの wrapper 経由で launcher を起動する。裸の
``python3`` を hooks.json へ書いていた頃は、Windows で ``python3`` が
Microsoft Store の App execution alias に解決され、launcher へ到達する前に
全 PreToolUse が hook error になっていた（release-verify 2026-09-03）。
インタプリタ解決の責務をこの 1 ファイルへ寄せた以上、解決順・見つからない
場合の終了コード・診断出力は機械的に固定する。

デシジョンテーブル（POSIX 側）:
  - launcher が存在しない            → 起動せず exit 0 + 診断（インタプリタ選択より前）
  - PLE4_PYTHON 設定あり            → そのインタプリタで launcher を起動
  - PLE4_PYTHON なし / python3 あり → python3 で起動（版の検査はしない）
  - python3 なし / python が 3.12+  → python で起動
  - python3 なし / python が 3.12 未満 → 起動せず exit 0 + 診断
  - どちらも無い                    → 起動せず exit 0 + 診断
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _PLUGIN_ROOT / "runtime" / "ple4-hook"
_WRAPPER_CMD = _PLUGIN_ROOT / "runtime" / "ple4-hook.cmd"

_PROTECTION_DISABLED_KEY = "ple4ProtectionDisabled"


def _write_stub(path: Path, body: str) -> None:
    """実行可能なシェルスタブを書き出す。

    Args:
        path: 作成するスタブのパス。
        body: シェバン以降のスクリプト本文。

    Returns:
        なし。
    """
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _run_wrapper(
    *args: str, env: dict[str, str], wrapper: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """wrapper を隔離環境で起動する。

    Args:
        *args: wrapper へ渡す引数。
        env: 子プロセスへ渡す環境変数一式。
        wrapper: 起動する wrapper のパス。省略時はリポジトリ内の実体。

    Returns:
        完了したプロセス。
    """
    return subprocess.run(
        [str(wrapper or _WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def _isolated_env(tmp_path: Path, *, path_dirs: list[Path]) -> dict[str, str]:
    """PATH を差し替えた最小環境を組み立てる。

    Args:
        tmp_path: 一時 HOME に使うディレクトリ。
        path_dirs: PATH に並べるディレクトリ（この順）。

    Returns:
        子プロセス用の環境変数。
    """
    return {
        "PATH": os.pathsep.join(str(d) for d in path_dirs),
        "HOME": str(tmp_path),
        "LANG": "C",
    }


def _wrapper_copy_without_launcher(tmp_path: Path) -> Path:
    """launcher を欠いた一時ツリーへ wrapper 本体だけを複製する。

    `<root>/runtime/ple4-hook` の配置だけを再現し、`<root>/src` は作らない。
    不完全インストール・Grok の hash symlink 切れと同じ状態になる。

    Args:
        tmp_path: 複製先のルートに使うディレクトリ。

    Returns:
        複製した wrapper のパス。
    """
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    copy = runtime_dir / _WRAPPER.name
    copy.write_bytes(_WRAPPER.read_bytes())
    copy.chmod(0o755)
    return copy


def test_wrapper_is_executable() -> None:
    """wrapper に実行ビットが立っていること（配布物でも exec できる前提）。"""
    assert os.access(_WRAPPER, os.X_OK)


def test_wrapper_fails_open_when_launcher_is_missing(tmp_path: Path) -> None:
    """launcher が無いとき exit 2 ではなく exit 0 + 診断で終わること（H-1）。

    fix 前は launcher の存在を確認せず `exec python3 "$_ple4_launcher"` して
    いた。`python3 <存在しないファイル>` は **exit 2** を返し、PreToolUse の
    「exit 2 = deny」契約により 4 つの保護 hook が**全ツール呼び出しを偽
    deny** する。fail-open（診断を出して exit 0）を掲げる設計と真逆の挙動で、
    しかも復旧手段の Bash ごと塞がるためセッション内から直せない。

    `PLE4_PYTHON` に**実在するインタプリタ**を渡しているのが要点。空 PATH で
    試すと `python_not_found` 側の分岐でも exit 0 になり、launcher 検査が
    インタプリタ選択より前にあることを証明できない。
    """
    env = _isolated_env(tmp_path, path_dirs=[])
    env["PLE4_PYTHON"] = sys.executable

    result = _run_wrapper(
        "ple4.hooks.pre_compact", env=env, wrapper=_wrapper_copy_without_launcher(tmp_path)
    )

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert result.stdout == ""
    diagnostic = json.loads(result.stderr.strip().splitlines()[-1])
    assert diagnostic[_PROTECTION_DISABLED_KEY] is True
    assert diagnostic["reason"] == "launcher_not_found"
    assert diagnostic["launcher"] == str(tmp_path / "src" / "ple4" / "launcher.py")


def test_wrapper_fails_open_when_invoked_without_a_path_separator(tmp_path: Path) -> None:
    """`$0` にスラッシュが無い起動でも偽 deny にならないこと（H-1）。

    `sh ple4-hook` のように引数へ直接渡すと `$0` は "ple4-hook" のままになり、
    wrapper の `*) _ple4_hook_dir=.` 分岐が root を `$PWD/..` として解決する。
    PATH 経由の起動ではこの分岐に入らない（カーネルが shebang スクリプトへ
    解決済みの絶対パスを渡すため `$0` にスラッシュが入る）ので、この形でしか
    到達しない。root 解決が cwd 依存に転んだ場合でも fail-open に落ちること、
    かつ診断に実際に探した launcher パスが載ることを固定する。
    """
    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    copy = bare_dir / _WRAPPER.name
    copy.write_bytes(_WRAPPER.read_bytes())
    copy.chmod(0o755)
    env = _isolated_env(tmp_path, path_dirs=[])
    env["PLE4_PYTHON"] = sys.executable

    result = subprocess.run(
        # PATH は空にしたまま（ambient な python3 を拾わせない）ので sh は絶対パスで呼ぶ。
        ["/bin/sh", copy.name, "ple4.hooks.pre_compact"],
        cwd=bare_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert result.stdout == ""
    diagnostic = json.loads(result.stderr.strip().splitlines()[-1])
    assert diagnostic["reason"] == "launcher_not_found"
    # cwd 起点で解決したことが診断から読めること（ファイルを消された場合と区別する）。
    assert diagnostic["launcher"] == str(tmp_path / "src" / "ple4" / "launcher.py")


def test_wrapper_uses_ple4_python_override(tmp_path: Path) -> None:
    """PLE4_PYTHON が最優先で使われ、launcher と対象モジュールまで到達する。"""
    env = _isolated_env(tmp_path, path_dirs=[])
    env["PLE4_PYTHON"] = sys.executable
    env["PLE4_LEARN_KIND"] = "fact"
    env["PLE4_LEARN_TITLE"] = "wrapper reached the module"

    result = _run_wrapper("ple4.mem.learn_payload", env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"kind": "fact", "title": "wrapper reached the module"}


def test_wrapper_prefers_python3_on_path(tmp_path: Path) -> None:
    """PLE4_PYTHON が無ければ PATH 上の python3 を版の検査なしで使う。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python3", 'printf "python3:%s\\n" "$1"\nexit 0')

    result = _run_wrapper("ple4.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout.startswith("python3:")
    assert result.stdout.strip().endswith("src/ple4/launcher.py")


def test_wrapper_falls_back_to_python_when_recent_enough(tmp_path: Path) -> None:
    """python3 が無く python が 3.12+ なら python で起動する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python", 'if [ "$1" = "-c" ]; then exit 0; fi\nprintf "python:%s\\n" "$1"')

    result = _run_wrapper("ple4.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout.startswith("python:")


def test_wrapper_rejects_python_older_than_312(tmp_path: Path) -> None:
    """python が 3.12 未満なら起動せず、診断だけを出して exit 0（fail-open）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python", 'if [ "$1" = "-c" ]; then exit 1; fi\nprintf "python:%s\\n" "$1"')

    result = _run_wrapper("ple4.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout == ""
    assert json.loads(result.stderr.strip().splitlines()[-1])[_PROTECTION_DISABLED_KEY] is True


def test_wrapper_reports_when_no_interpreter_exists(tmp_path: Path) -> None:
    """インタプリタが 1 つも無ければ exit 0 + 構造化診断（stdout は汚さない）。

    fail-closed（非 0）にすると保護 hook が全 Bash/Edit を拒否し、PATH を
    直す手段ごとセッション内から塞がれる。launcher.py の未対応 Python 時と
    同じ fail-open ポリシーに揃える（CLAUDE.md「ランタイム前提」）。
    """
    result = _run_wrapper("ple4.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[]))

    assert result.returncode == 0
    assert result.stdout == ""
    diagnostic = json.loads(result.stderr.strip().splitlines()[-1])
    assert diagnostic[_PROTECTION_DISABLED_KEY] is True
    assert diagnostic["reason"] == "python_not_found"
    assert diagnostic["requiredVersion"] == "3.12+"


def _cmd_lines() -> list[str]:
    """Windows wrapper の実行行（コメント・空行を除く）を返す。

    Returns:
        `rem` コメントと空行を落とした行のリスト。
    """
    return [
        line.strip()
        for line in _WRAPPER_CMD.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().lower().startswith("rem ")
    ]


def test_windows_wrapper_shares_the_posix_contract() -> None:
    """Windows 版 .cmd が POSIX 版と同じ解決順・同じ fail-open を宣言していること。

    Windows 実機では実行できないため、契約に相当する要素の宣言をテキストとして
    検査する。ここが崩れると、Windows だけ別の（検証されていない）挙動になる。
    """
    text = _WRAPPER_CMD.read_text(encoding="utf-8")

    assert "%PLE4_PYTHON%" in text
    assert "call :ple4_pick py -3\n" in text
    assert "call :ple4_pick python\n" in text
    assert "call :ple4_pick python3\n" in text
    assert '"%PLE4_LAUNCHER%"' in text
    assert _PROTECTION_DISABLED_KEY in text
    assert "exit /b 0" in text


def test_windows_wrapper_checks_the_launcher_before_choosing_an_interpreter() -> None:
    """launcher 不在の検査がインタプリタ選択より前にあること（H-1 の Windows 側）。

    POSIX 側だけ塞ぐと、同じ不完全インストールが Windows でだけ exit 2 の
    偽 deny になる。順序が契約の本体なので、宣言の有無ではなく**位置**を見る。
    """
    lines = _cmd_lines()
    guard = next(i for i, line in enumerate(lines) if line.startswith("if not exist"))
    first_pick = next(i for i, line in enumerate(lines) if line.startswith("call :ple4_pick"))
    pythonenv = next(i for i, line in enumerate(lines) if line.startswith("if defined PLE4_PYTHON"))

    assert guard < pythonenv < first_pick
    assert "%PLE4_LAUNCHER%" in lines[guard]
    assert any("launcher_not_found" in line for line in lines)
    # 診断行より後ろに素の exit /b 0 があり、deny(2) に化けないこと。
    diag = next(i for i, line in enumerate(lines) if "launcher_not_found" in line)
    assert "exit /b 0" in lines[diag + 1 :]


def test_windows_wrapper_probes_each_candidate_with_its_exec_arguments() -> None:
    """採用前に 3.12+ probe を通し、probe と exec の引数が一致すること（H-18b）。

    `py.exe` だけがあり Python 3 が未登録の host では `py -3` が非ゼロで終わる。
    probe が無いとその候補のまま exec し、launcher が一度も動かないまま
    診断も出ず、PATH 上の正常な python3 も試されない。
    probe を `%PLE4_PYARGS%` 付きで行うのは、bare `py` と `py -3` が別の
    ランタイムを選びうるため（probe した物と exec する物を一致させる）。
    """
    lines = _cmd_lines()
    probe = next(line for line in lines if "sys.version_info" in line)

    assert probe.startswith('"%PLE4_PY%"')
    assert "%PLE4_PYARGS%" in probe
    assert "(3, 12)" in probe
    # 失敗した候補は捨てられ、次の候補名へ進めること。
    assert 'if errorlevel 1 set "PLE4_PY="' in lines


def test_windows_wrapper_never_expands_errorlevel_after_running_python() -> None:
    """`exit /b %ERRORLEVEL%` を持たないこと（cmd の parse 時展開で deny が消える）。

    cmd は `if (...)` ブロック内の `%VAR%` を**パース時**に展開する。
    ブロック内で python を起動して `exit /b %ERRORLEVEL%` と書くと、返るのは
    起動**前**の errorlevel になる。保護 hook の exit 2 が 0 として host へ
    報告され、Windows でだけ全ブロックが素通りする（P1-004 と同じ失敗クラス）。
    素の `exit /b` は現在の errorlevel を保つ。

    禁じるのは `%ERRORLEVEL%` の**パーセント展開**だけで、`if errorlevel N`
    ビルトインは対象外。後者は実行時に比較されるためこの落とし穴を持たず、
    probe の結果を読む唯一の手段でもある。
    """
    # コメントは対象外（この落とし穴の説明そのものが本文に書いてある）。
    lines = _cmd_lines()

    assert not any("%ERRORLEVEL%" in line.upper() for line in lines)
    # launcher を起動する行の直後は素の `exit /b`（probe 行と取り違えない）。
    exec_index = next(
        i
        for i, line in enumerate(lines)
        if line.startswith('"%PLE4_PY%"') and "%PLE4_LAUNCHER%" in line
    )
    assert lines[exec_index + 1] == "exit /b"


def test_windows_wrapper_launches_python_outside_any_block() -> None:
    """インタプリタ起動行がカッコブロックの中に無いこと。

    ブロック内で起動すると %ERRORLEVEL% の parse 時展開に戻ってしまうため、
    「ブロックを使わない」こと自体を契約として固定する。
    """
    depth = 0
    for line in _cmd_lines():
        if line.startswith('"%PLE4_PY%"') or line.startswith('"%PLE4_PYTHON%"'):
            assert depth == 0, f"インタプリタ起動がブロック内にある: {line}"
        depth += line.count("(") - line.count(")")


def test_windows_wrapper_filters_store_alias_inside_the_where_pipeline() -> None:
    """WindowsApps 除外を `where` のパイプライン側で行うこと。

    for 変数は展開後に再パースされるため、`echo %%P| findstr ...` 形式だと
    `C:\\Program Files (x86)\\...` のようなカッコ入りの実在パスでブロックが
    壊れる。あわせて findstr のパターンが閉じ引用符直前でバックスラッシュを
    終端しないことも見る（C ランタイムが `\\"` を引用符のエスケープとして
    読み、パターンが永久に一致しなくなるため）。
    """
    lines = _cmd_lines()
    pick = next(line for line in lines if line.startswith("for /f") and "where " in line)

    assert "^| findstr /i /v /c:" in pick
    assert '"\\WindowsApps"' in pick
    assert '\\"' not in pick
    assert "echo" not in pick


def test_windows_wrapper_resolves_candidates_from_path_only() -> None:
    """`where` の検索範囲を PATH に限定すること（H-18a）。

    Windows の `where` は仕様上 **PATH より先にカレントディレクトリ**を見る。
    hook は cwd=プロジェクトルートで起動するため、リポジトリ直下に置かれた
    `python.exe` が 1 件目として採用され、hook の stdin（ツール入力 JSON）ごと
    そのバイナリへ渡る。POSIX 側の `command -v` は cwd を見ないので、これは
    Windows だけが開いている穴になる。`$PATH:` 形式で解決範囲を揃える。

    darwin 上で cmd.exe を実行できないため、実起動ではなく形の検査で固定する。
    """
    pick = next(line for line in _cmd_lines() if line.startswith("for /f") and "where " in line)

    assert 'where "$PATH:%1.exe"' in pick
    assert "where %1" not in pick
