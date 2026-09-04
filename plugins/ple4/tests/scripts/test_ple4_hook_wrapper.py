"""runtime/ple4-hook（および Windows 版 .cmd）の起動契約テスト。

hooks.json の全エントリはこの wrapper 経由で launcher を起動する。裸の
``python3`` を hooks.json へ書いていた頃は、Windows で ``python3`` が
Microsoft Store の App execution alias に解決され、launcher へ到達する前に
全 PreToolUse が hook error になっていた（release-verify 2026-09-03）。
インタプリタ解決の責務をこの 1 ファイルへ寄せた以上、解決順・見つからない
場合の終了コード・診断出力は機械的に固定する。

デシジョンテーブル（POSIX 側）:
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


def _run_wrapper(*args: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    """wrapper を隔離環境で起動する。

    Args:
        *args: wrapper へ渡す引数。
        env: 子プロセスへ渡す環境変数一式。

    Returns:
        完了したプロセス。
    """
    return subprocess.run(
        [str(_WRAPPER), *args],
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


def test_wrapper_is_executable() -> None:
    """wrapper に実行ビットが立っていること（配布物でも exec できる前提）。"""
    assert os.access(_WRAPPER, os.X_OK)


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
    assert "call :ple4_pick py\n" in text
    assert "call :ple4_pick python\n" in text
    assert "call :ple4_pick python3\n" in text
    assert '"%PLE4_LAUNCHER%"' in text
    assert _PROTECTION_DISABLED_KEY in text
    assert "exit /b 0" in text


def test_windows_wrapper_never_expands_errorlevel_after_running_python() -> None:
    """`exit /b %ERRORLEVEL%` を持たないこと（cmd の parse 時展開で deny が消える）。

    cmd は `if (...)` ブロック内の `%VAR%` を**パース時**に展開する。
    ブロック内で python を起動して `exit /b %ERRORLEVEL%` と書くと、返るのは
    起動**前**の errorlevel になる。保護 hook の exit 2 が 0 として host へ
    報告され、Windows でだけ全ブロックが素通りする（P1-004 と同じ失敗クラス）。
    素の `exit /b` は現在の errorlevel を保つ。
    """
    # コメントは対象外（この落とし穴の説明そのものが本文に書いてある）。
    lines = _cmd_lines()

    assert not any("ERRORLEVEL" in line.upper() for line in lines)
    # インタプリタ起動行の直後は素の `exit /b`。
    exec_index = next(i for i, line in enumerate(lines) if line.startswith('"%PLE4_PY%"'))
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
    pick = next(line for line in lines if line.startswith("for /f") and "where %1" in line)

    assert "^| findstr /i /v /c:" in pick
    assert '"\\WindowsApps"' in pick
    assert '\\"' not in pick
    assert "echo" not in pick
