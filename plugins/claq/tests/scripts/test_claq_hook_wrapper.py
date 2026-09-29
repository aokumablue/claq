"""runtime/claq-hook（および Windows 版 .cmd）の起動契約テスト。

hooks.json の全エントリはこの wrapper 経由で launcher を起動する。裸の
``python3`` を hooks.json へ書いていた頃は、Windows で ``python3`` が
Microsoft Store の App execution alias に解決され、launcher へ到達する前に
全 PreToolUse が hook error になっていた（release-verify 2026-09-03）。
インタプリタ解決の責務をこの 1 ファイルへ寄せた以上、解決順・見つからない
場合の終了コード・診断出力は機械的に固定する。

デシジョンテーブル（POSIX 側）:
  - launcher が存在しない/読めない    → 起動せず exit 0 + 診断（インタプリタ選択より前）
  - PATH に絶対エントリが 1 つも無い  → 起動せず exit 0 + 診断（CLAQ_PYTHON より前）
  - CLAQ_PYTHON が絶対パス          → そのインタプリタで launcher を起動
  - CLAQ_PYTHON が相対名/相対パス    → 起動せず exit 0 + 診断（PATH 探索へ落とさない）
  - CLAQ_PYTHON なし / python3 あり → python3 で起動（版の検査はしない）
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

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _PLUGIN_ROOT / "runtime" / "claq-hook"
_WRAPPER_CMD = _PLUGIN_ROOT / "runtime" / "claq-hook.cmd"

_PROTECTION_DISABLED_KEY = "claqProtectionDisabled"

# cwd 由来の偽インタプリタが動いたことだけを示す印。実際の攻撃はここで hook の
# stdin（ツール入力 JSON）を読み、exit code も自由に決められる。
_HIJACK_MARKER = "CLAQ-HIJACKED"


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


def _write_hijack_stub(path: Path) -> None:
    """採用されたら分かる偽インタプリタを書き出す。

    Args:
        path: 作成するスタブのパス。

    Returns:
        なし。
    """
    _write_stub(path, f'printf "{_HIJACK_MARKER}\\n"\nexit 0')


def _run_wrapper(
    *args: str,
    env: dict[str, str],
    wrapper: Path | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """wrapper を隔離環境で起動する。

    Args:
        *args: wrapper へ渡す引数。
        env: 子プロセスへ渡す環境変数一式。
        wrapper: 起動する wrapper のパス。省略時はリポジトリ内の実体。
        cwd: 子プロセスの作業ディレクトリ。省略時は呼び出し元のまま。

    Returns:
        完了したプロセス。
    """
    return subprocess.run(
        [str(wrapper or _WRAPPER), *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=None if cwd is None else str(cwd),
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
    return _isolated_env_raw(tmp_path, path=os.pathsep.join(str(d) for d in path_dirs))


def _isolated_env_raw(tmp_path: Path, *, path: str) -> dict[str, str]:
    """PATH を文字列のまま指定して最小環境を組み立てる。

    空要素や `.` を含む PATH は `os.pathsep.join` では表現しづらいので、
    生の文字列を受け取る入口を分ける。

    Args:
        tmp_path: 一時 HOME に使うディレクトリ。
        path: そのまま子プロセスへ渡す PATH の値。

    Returns:
        子プロセス用の環境変数。
    """
    return {"PATH": path, "HOME": str(tmp_path), "LANG": "C"}


def _diagnostic(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """stderr 末尾の構造化診断行を読む。

    Args:
        result: wrapper の実行結果。

    Returns:
        パースした診断 JSON。
    """
    return json.loads(result.stderr.strip().splitlines()[-1])


def _hijack_tree(tmp_path: Path) -> tuple[Path, Path]:
    """「悪意あるリポジトリ」と「PATH 上の唯一の絶対ディレクトリ」を用意する。

    偽インタプリタは cwd 側にだけ置き、PATH 側の絶対ディレクトリは**空**に
    する。こうしないと `/usr/bin/python3` のような実在候補が順序で勝ってしまい、
    修正が無くてもテストが緑になる（実測でその取り違えを確認済み）。

    Args:
        tmp_path: ツリーを作る一時ディレクトリ。

    Returns:
        (cwd に使う偽 python3 入りディレクトリ, 空の絶対 PATH ディレクトリ)。

    """
    repo = tmp_path / "hostile-repo"
    repo.mkdir()
    _write_hijack_stub(repo / "python3")
    _write_hijack_stub(repo / "python")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    return repo, empty


def _wrapper_copy_without_launcher(tmp_path: Path) -> Path:
    """launcher を欠いた一時ツリーへ wrapper 本体だけを複製する。

    `<root>/runtime/claq-hook` の配置だけを再現し、`<root>/src` は作らない。
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

    fix 前は launcher の存在を確認せず `exec python3 "$_claq_launcher"` して
    いた。`python3 <存在しないファイル>` は **exit 2** を返し、PreToolUse の
    「exit 2 = deny」契約により 4 つの保護 hook が**全ツール呼び出しを偽
    deny** する。fail-open（診断を出して exit 0）を掲げる設計と真逆の挙動で、
    しかも復旧手段の Bash ごと塞がるためセッション内から直せない。

    `CLAQ_PYTHON` に**実在するインタプリタ**を渡しているのが要点。使えない
    PATH で試すと後段の分岐でも exit 0 になり、launcher 検査がインタプリタ
    選択より前にあることを証明できない。
    """
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _isolated_env(tmp_path, path_dirs=[empty_bin])
    env["CLAQ_PYTHON"] = sys.executable

    result = _run_wrapper(
        "claq.hooks.pre_compact", env=env, wrapper=_wrapper_copy_without_launcher(tmp_path)
    )

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert result.stdout == ""
    assert _diagnostic(result) == {
        _PROTECTION_DISABLED_KEY: True,
        "reason": "launcher_not_found",
    }
    # 探したパスは人間可読行だけに載る（JSON へ生連結すると壊れる。F を参照）。
    assert str(tmp_path / "src" / "claq" / "launcher.py") in result.stderr.splitlines()[0]


def test_wrapper_fails_open_when_launcher_is_unreadable(tmp_path: Path) -> None:
    """存在するが読めない launcher でも偽 deny にならないこと。

    `python3 <読めないファイル>` も **exit 2** を返すので、不在と同じ経路へ
    倒す必要がある。存在検査だけを入れて可読性検査を落とすと、パーミッション
    事故がそのまま「全ツール呼び出しの偽 deny」に化ける。
    """
    copy = _wrapper_copy_without_launcher(tmp_path)
    launcher = tmp_path / "src" / "claq" / "launcher.py"
    launcher.parent.mkdir(parents=True)
    launcher.write_text("raise SystemExit(0)\n", encoding="utf-8")
    launcher.chmod(0o000)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _isolated_env(tmp_path, path_dirs=[empty_bin])
    env["CLAQ_PYTHON"] = sys.executable

    try:
        result = _run_wrapper("claq.hooks.pre_compact", env=env, wrapper=copy)
    finally:
        launcher.chmod(0o644)

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert _diagnostic(result)["reason"] == "launcher_not_found"


def test_wrapper_fails_open_when_invoked_without_a_path_separator(tmp_path: Path) -> None:
    """`$0` にスラッシュが無い起動でも偽 deny にならないこと（H-1）。

    `sh claq-hook` のように引数へ直接渡すと `$0` は "claq-hook" のままになり、
    wrapper の `*) _claq_hook_dir=.` 分岐が root を `$PWD/..` として解決する。
    PATH 経由の起動ではこの分岐に入らない（カーネルが shebang スクリプトへ
    解決済みの絶対パスを渡すため `$0` にスラッシュが入る）ので、この形でしか
    到達しない。root 解決が cwd 依存に転んだ場合でも fail-open に落ちること、
    かつ人間可読行に実際に探した launcher パスが載ることを固定する。
    """
    bare_dir = tmp_path / "bare"
    bare_dir.mkdir()
    copy = bare_dir / _WRAPPER.name
    copy.write_bytes(_WRAPPER.read_bytes())
    copy.chmod(0o755)
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _isolated_env(tmp_path, path_dirs=[empty_bin])
    env["CLAQ_PYTHON"] = sys.executable

    result = subprocess.run(
        # PATH には空の絶対ディレクトリだけを置く（ambient な python3 を拾わせない）。
        ["/bin/sh", copy.name, "claq.hooks.pre_compact"],
        cwd=bare_dir,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert result.stdout == ""
    assert _diagnostic(result)["reason"] == "launcher_not_found"
    # cwd 起点で解決したことが人間可読行から読めること（ファイルを消された場合と区別する）。
    assert str(tmp_path / "src" / "claq" / "launcher.py") in result.stderr.splitlines()[0]


def test_launcher_diagnostics_never_embed_a_path_in_json() -> None:
    """診断 JSON にパスを載せないこと（F）。

    POSIX のパスは `"`・`\\`・改行を全て含んでよい。生のまま JSON へ連結すると
    引用符や `\\U` で壊れ、改行を含むパスなら**偽の構造化行を 1 本注入**できる
    （`{"claqProtectionDisabled": false}` を後続行として差し込む等）。移植可能な
    エスケープ手段が無い（`${var//}` は bash 専用、`sed` は PATH が壊れている
    この状況では呼べない）ので、パスは人間可読行だけに載せる。Windows 側も
    同じ理由で `%CLAQ_LAUNCHER_JSON%` ごと落としてある（片側だけ塞がない）。
    """
    posix = _WRAPPER.read_text(encoding="utf-8")
    windows = _WRAPPER_CMD.read_text(encoding="utf-8")

    posix_json = [line for line in posix.splitlines() if _PROTECTION_DISABLED_KEY in line]
    windows_json = [
        line
        for line in windows.splitlines()
        if _PROTECTION_DISABLED_KEY in line and not line.strip().lower().startswith("rem")
    ]

    assert posix_json and windows_json
    # 検査するのは変数**展開**であって、候補名としての文字列ではない
    # （`"candidates": ["CLAQ_PYTHON", ...]` は固定文字列なので安全）。
    for line in posix_json:
        assert "$_claq_launcher" not in line
        assert "$CLAQ_PYTHON" not in line
        assert "${CLAQ_PYTHON" not in line
    for line in windows_json:
        assert "%CLAQ_LAUNCHER" not in line
        assert "%CLAQ_PYTHON%" not in line
    # 使われなくなった JSON 用のパス変数ごと消えていること。
    assert "CLAQ_LAUNCHER_JSON" not in windows


def test_wrapper_uses_claq_python_override(tmp_path: Path) -> None:
    """CLAQ_PYTHON（絶対パス）が最優先で使われ、launcher と対象モジュールまで到達する。"""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    env = _isolated_env(tmp_path, path_dirs=[empty_bin])
    env["CLAQ_PYTHON"] = sys.executable
    env["CLAQ_LEARN_KIND"] = "fact"
    env["CLAQ_LEARN_TITLE"] = "wrapper reached the module"

    result = _run_wrapper("claq.mem.learn_payload", env=env)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"kind": "fact", "title": "wrapper reached the module"}


def test_wrapper_prefers_python3_over_python_when_both_exist(tmp_path: Path) -> None:
    """両方が PATH にあるとき `python3` が勝つこと（CLAUDE.md 規定の順序）。

    既存の 2 件はそれぞれ片方だけを stub するため、実装の順序を入れ替えても
    どちらも緑のままだった。順序そのものを踏む対照がここまで無かった。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python3", 'printf "PY3-WON\\n"\nexit 0')
    _write_stub(bin_dir / "python", 'printf "PY-WON\\n"\nexit 0')

    result = _run_wrapper("claq.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert "PY3-WON" in result.stdout, result.stdout
    assert "PY-WON" not in result.stdout


def test_wrapper_prefers_python3_on_path(tmp_path: Path) -> None:
    """CLAQ_PYTHON が無ければ PATH 上の python3 を版の検査なしで使う。

    A の修正（PATH 洗浄）が「PATH を空にするだけ」に退化していないことの
    陽性対照でもある。ここが赤くなれば、下の乗っ取りテスト群が緑でも意味がない。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python3", 'printf "python3:%s\\n" "$1"\nexit 0')

    result = _run_wrapper("claq.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout.startswith("python3:")
    assert result.stdout.strip().endswith("src/claq/launcher.py")


def test_wrapper_falls_back_to_python_when_recent_enough(tmp_path: Path) -> None:
    """python3 が無く python が 3.12+ なら python で起動する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python", 'if [ "$1" = "-c" ]; then exit 0; fi\nprintf "python:%s\\n" "$1"')

    result = _run_wrapper("claq.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout.startswith("python:")


def test_wrapper_rejects_python_older_than_312(tmp_path: Path) -> None:
    """python が 3.12 未満なら起動せず、診断だけを出して exit 0（fail-open）。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python", 'if [ "$1" = "-c" ]; then exit 1; fi\nprintf "python:%s\\n" "$1"')

    result = _run_wrapper("claq.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[bin_dir]))

    assert result.returncode == 0
    assert result.stdout == ""
    assert _diagnostic(result)[_PROTECTION_DISABLED_KEY] is True


def test_wrapper_probe_does_not_consume_the_hook_payload(tmp_path: Path) -> None:
    """3.12+ probe が hook の stdin を食わないこと（G）。

    probe は `python -c` を起動する。stdin を閉じずに起動すると、コード側が
    読みさえすれば**ツール入力 JSON がそこで消費され**、本体は空の payload を
    読むことになる（保護 hook は payload が無ければ検査対象を失う）。現状の
    `python -c` が読まないから安全、というのは実装の振る舞いへの依存でしか
    ないので、`</dev/null` で構造として保証していることを実測で固定する。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(
        bin_dir / "python",
        'if [ "$1" = "-c" ]; then cat >/dev/null; exit 0; fi\nprintf "payload=[%s]\\n" "$(cat)"',
    )
    # スタブは `cat` を呼ぶので実在の絶対ディレクトリも PATH へ足す。洗浄は
    # フィルタであって置き換えではないことの確認も兼ねる（/bin に python は無い）。
    result = subprocess.run(
        [str(_WRAPPER), "claq.hooks.pre_compact"],
        input='{"tool_name":"Bash"}',
        capture_output=True,
        text=True,
        env=_isolated_env(tmp_path, path_dirs=[bin_dir, Path("/bin")]),
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'payload=[{"tool_name":"Bash"}]'


def test_wrapper_reports_when_no_interpreter_exists(tmp_path: Path) -> None:
    """インタプリタが 1 つも無ければ exit 0 + 構造化診断（stdout は汚さない）。

    fail-closed（非 0）にすると保護 hook が全 Bash/Edit を拒否し、PATH を
    直す手段ごとセッション内から塞がれる。launcher.py の未対応 Python 時と
    同じ fail-open ポリシーに揃える（CLAUDE.md「ランタイム前提」）。
    """
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()

    result = _run_wrapper("claq.hooks.pre_compact", env=_isolated_env(tmp_path, path_dirs=[empty_bin]))

    assert result.returncode == 0
    assert result.stdout == ""
    diagnostic = _diagnostic(result)
    assert diagnostic[_PROTECTION_DISABLED_KEY] is True
    assert diagnostic["reason"] == "python_not_found"
    assert diagnostic["requiredVersion"] == "3.12+"


# --- A: cwd 由来インタプリタの採用（実測で悪用可能だった経路） -------------------
#
# POSIX は PATH の長さ 0 の要素（先頭 `:`・末尾 `:`・連続 `::`）と `.` を
# 「カレントディレクトリ」と規定する。hook は cwd=プロジェクトルートで起動する
# ため、リポジトリが同梱した `./python3` が `command -v python3` と
# `exec python3` の両方で採用され、hook の stdin（ツール入力 JSON）を受け取り
# exit code まで支配できていた（= 保護の完全な乗っ取り。修正前に実測で再現）。
#
# 表の各行は「空要素/`.`/相対エントリが**唯一解決しうるエントリ**」になるよう
# 組む。`/usr/bin` のような実在ディレクトリを同居させると、そちらが順序で勝って
# 修正が無くても緑になる（実測でこの取り違えを確認済み）。
@pytest.mark.parametrize(
    ("label", "path_template"),
    [
        ("先頭の空要素", ":{empty}"),
        ("末尾の空要素", "{empty}:"),
        ("連続する空要素", "{empty}::"),
        ("明示的な .", ".:{empty}"),
        ("末尾の .", "{empty}:."),
        ("相対ディレクトリ", "relative/bin:{empty}"),
    ],
)
def test_wrapper_never_adopts_a_cwd_interpreter(
    tmp_path: Path, label: str, path_template: str
) -> None:
    """cwd の `python3` が PATH の空要素・`.`・相対エントリ経由で採用されないこと。

    Args:
        tmp_path: 合成ツリーを作る一時ディレクトリ。
        label: 失敗時に PATH の形を読めるようにする表のラベル。
        path_template: `{empty}` に空の絶対ディレクトリを埋める PATH のひな形。

    Returns:
        なし。
    """
    repo, empty = _hijack_tree(tmp_path)
    env = _isolated_env_raw(tmp_path, path=path_template.format(empty=empty))

    result = _run_wrapper("claq.hooks.pre_compact", env=env, cwd=repo)

    assert _HIJACK_MARKER not in result.stdout, f"{label}: cwd のインタプリタが動いた"
    assert _HIJACK_MARKER not in result.stderr, f"{label}: cwd のインタプリタが動いた"
    assert result.returncode == 0, f"{label}: fail-open していない"
    assert _diagnostic(result)["reason"] == "python_not_found", label


def test_wrapper_refuses_a_path_without_any_absolute_entry(tmp_path: Path) -> None:
    """絶対エントリが 1 つも無い PATH では hook を起動しないこと。

    洗浄後に何も残らない PATH の下で hook を動かすと、hook 自身の
    `subprocess.run(["git", ...])` が cwd の `./git` に解決される（execvp も
    空要素を cwd として扱う）。攻撃者が得るのは「保護のバイパス」ではなく
    「hook 権限での任意コード実行」になるので、明確に軽い前者へ倒す。
    CLAQ_PYTHON が絶対パスでもこの判断は変わらない。
    """
    repo, _ = _hijack_tree(tmp_path)
    env = _isolated_env_raw(tmp_path, path=".:relative/bin:")
    env["CLAQ_PYTHON"] = sys.executable

    result = _run_wrapper("claq.hooks.pre_compact", env=env, cwd=repo)

    assert result.returncode == 0
    assert _HIJACK_MARKER not in result.stdout
    assert _diagnostic(result) == {
        _PROTECTION_DISABLED_KEY: True,
        "reason": "path_not_absolute",
    }


def test_wrapper_exports_a_sanitized_path_to_the_interpreter(tmp_path: Path) -> None:
    """洗浄した PATH が子プロセスへ export されること。

    元の PATH をそのまま渡し直すと、`./python3` を塞いだ端から hook 本体の
    `git` 解決が `./git` に落ちて同じ乗っ取りが成立する（hook_common.py と
    commit_quality_scanner.py はどちらも名前で git を起動する）。wrapper の
    責務をここだけ「インタプリタを解決する」から「信頼できる PATH の下で
    解決する」へ意図的に広げているので、その決定をテストで固定する。
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_stub(bin_dir / "python3", 'printf "PATH=[%s]\\n" "$PATH"\nexit 0')
    env = _isolated_env_raw(tmp_path, path=f":{bin_dir}:.:relative/bin:")

    result = _run_wrapper("claq.hooks.pre_compact", env=env, cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"PATH=[{bin_dir}]"


# --- B: CLAQ_PYTHON の相対値 ---------------------------------------------------
@pytest.mark.parametrize(
    ("label", "value"),
    [
        ("裸の名前", "python3"),
        ("明示的な相対パス", "./python3"),
        ("サブディレクトリ相対", "bin/python3"),
        ("親ディレクトリ相対", "../hostile-repo/python3"),
    ],
)
def test_wrapper_rejects_a_relative_claq_python(tmp_path: Path, label: str, value: str) -> None:
    """CLAQ_PYTHON が相対なら cwd のインタプリタを絶対に起動しないこと。

    `CLAQ_PYTHON=./python3` は POSIX 側でも実測で乗っ取りに成立した（監査は
    裸の名前しか試しておらず「POSIX では再現せず」と報告していた）。裸の名前も
    PATH に空要素があれば同じ穴になる。PATH 探索へ**落とさない**のも契約で、
    明示的な override が黙って別のインタプリタに化けるのは利用者の前提を破る。

    Args:
        tmp_path: 合成ツリーを作る一時ディレクトリ。
        label: 失敗時に値の形を読めるようにする表のラベル。
        value: CLAQ_PYTHON へ入れる相対値。

    Returns:
        なし。
    """
    repo, empty = _hijack_tree(tmp_path)
    (repo / "bin").mkdir()
    _write_hijack_stub(repo / "bin" / "python3")
    # PATH 側には正規の python3 を置く。「PATH へ落ちて別物が動く」ことも防ぐ。
    _write_stub(empty / "python3", 'printf "FELL-THROUGH-TO-PATH\\n"\nexit 0')
    env = _isolated_env(tmp_path, path_dirs=[empty])
    env["CLAQ_PYTHON"] = value

    result = _run_wrapper("claq.hooks.pre_compact", env=env, cwd=repo)

    assert _HIJACK_MARKER not in result.stdout, f"{label}: cwd のインタプリタが動いた"
    assert "FELL-THROUGH-TO-PATH" not in result.stdout, f"{label}: PATH 探索へ落ちた"
    assert result.returncode == 0, label
    assert _diagnostic(result)["reason"] == "claq_python_not_absolute", label


def _cmd_lines() -> list[str]:
    """Windows wrapper の実行行（コメント・空行を除く）を返す。

    Returns:
        `rem` コメント（区切りの裸 `rem` を含む）と空行を落とした行のリスト。
    """
    lines = []
    for raw in _WRAPPER_CMD.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        lowered = line.lower()
        if not line or lowered == "rem" or lowered.startswith("rem "):
            continue
        lines.append(line)
    return lines


def test_windows_wrapper_shares_the_posix_contract() -> None:
    """Windows 版 .cmd が POSIX 版と同じ解決順・同じ fail-open を宣言していること。

    Windows 実機では実行できないため、契約に相当する要素の宣言をテキストとして
    検査する。ここが崩れると、Windows だけ別の（検証されていない）挙動になる。
    """
    text = _WRAPPER_CMD.read_text(encoding="utf-8")

    assert "%CLAQ_PYTHON%" in text
    # 存在だけでなく**順序**を固定する。CLAUDE.md は Windows 側の解決順を
    # `CLAQ_PYTHON` > `py -3` > `python` > `python3` と規定しており、
    # 存在検査だけでは `python` と `python3` を入れ替えても緑のままになる。
    picks = ["call :claq_pick py -3\n", "call :claq_pick python\n", "call :claq_pick python3\n"]
    indexes = [text.find(pick) for pick in picks]
    assert all(index != -1 for index in indexes), picks
    assert indexes == sorted(indexes), f"インタプリタ解決順が CLAUDE.md 規定と違う: {indexes}"
    assert '"%CLAQ_LAUNCHER%"' in text
    assert _PROTECTION_DISABLED_KEY in text
    assert "exit /b 0" in text


def test_both_wrappers_use_the_same_diagnostic_reasons() -> None:
    """両 OS で共通の失敗理由が同じ文字列で出ること。

    「片方だけ塞ぐ」再発（本レビューで 5 回）を機械的に止めるための対称性
    ガード。散文のレビューより強い。

    PATH 由来の穴は両 OS で塞いであるが、**塞ぎ方が違うので理由文字列も違う**:
    POSIX は PATH の非絶対要素を除去して続行する（`path_not_absolute`）。cmd は
    除去に per-entry ループ＝遅延展開が要り、`!` を含む Windows パスを壊すため
    採れないので、零長要素を**検出して** fail-open する（`path_has_empty_entry`）。
    残る非対称（cmd 側は非空の相対要素を捕まえられない）は .cmd の
    KNOWN RESIDUAL に理由付きで明記してある。
    """
    posix = _WRAPPER.read_text(encoding="utf-8")
    windows = _WRAPPER_CMD.read_text(encoding="utf-8")

    for reason in ("launcher_not_found", "python_not_found", "claq_python_not_absolute"):
        assert reason in posix, reason
        assert reason in windows, reason
    # PATH 由来の穴は両側に手当てがあること（片側だけ無防備にしない）。
    assert "path_not_absolute" in posix
    assert "path_has_empty_entry" in windows
    # 残る非対称と、この環境で実行検証できない旨が明記されていること。
    assert "KNOWN RESIDUAL" in windows
    assert "UNVERIFIED FROM THIS HOST" in windows


def test_windows_wrapper_checks_the_launcher_before_choosing_an_interpreter() -> None:
    """launcher 不在/不読の検査がインタプリタ選択より前にあること（H-1 の Windows 側）。

    POSIX 側だけ塞ぐと、同じ不完全インストールが Windows でだけ exit 2 の
    偽 deny になる。順序が契約の本体なので、宣言の有無ではなく**位置**を見る。
    可読性検査（D）も同じ位置に要る: POSIX 側は `[ -f ]` と `[ -r ]` を対で
    見ており、`type` を欠くと「存在するが読めない launcher」が Windows でだけ
    exit 2 の偽 deny に戻る。
    """
    lines = _cmd_lines()
    guard = next(i for i, line in enumerate(lines) if line.startswith("if not exist"))
    readable = next(i for i, line in enumerate(lines) if line.startswith('type "%CLAQ_LAUNCHER%"'))
    first_pick = next(i for i, line in enumerate(lines) if line.startswith("call :claq_pick"))
    pythonenv = next(i for i, line in enumerate(lines) if line.startswith("if defined CLAQ_PYTHON"))

    assert guard < readable < pythonenv < first_pick
    assert "%CLAQ_LAUNCHER%" in lines[guard]
    # 読めない launcher も不在と同じ診断経路へ倒すこと。
    assert lines[readable + 1] == "if errorlevel 1 goto :claq_no_launcher"
    assert lines[guard].endswith("goto :claq_no_launcher")
    assert any("launcher_not_found" in line for line in lines)
    # 診断行より後ろに素の exit /b 0 があり、deny(2) に化けないこと。
    diag = next(i for i, line in enumerate(lines) if "launcher_not_found" in line)
    assert "exit /b 0" in lines[diag + 1 :]


def test_windows_wrapper_detects_empty_path_entries() -> None:
    """PATH の空要素を検出して fail-open へ倒す分岐があること（形の検査）。

    cmd.exe は零長の PATH 要素（``;;``・先頭 ``;``・末尾 ``;``）を**現在
    ディレクトリ**として解決する。フックはプロジェクトルートを cwd に起動するので、
    構造ルール 4 の ``where "$PATH:..."`` を使っていても、PATH 自身が相対
    ディレクトリを名指ししていれば cwd の python.exe に届いてしまう。

    **このテストは形だけを固定する。** darwin から cmd.exe を実行できないため、
    検出が実際に発火することは観測できていない。固定しているのは
    「検査が存在し、既定は未設定で、一致したときだけ立ち、立ったら
    ``:claq_path_unsafe`` へ飛ぶ」という構造であって、振る舞いの検証ではない。

    構造が重要なのは失敗の向きを縛るため: 検査行が壊れて解釈できなくても
    ``CLAQ_PATH_RISK`` は未設定のまま残り、解決は従来どおり進む。壊れた検査が
    「Windows で保護が無効」へ倒れることはない。
    """
    lines = _cmd_lines()
    reset = next(i for i, line in enumerate(lines) if line == 'set "CLAQ_PATH_RISK="')
    branch = next(i for i, line in enumerate(lines) if line == "if defined CLAQ_PATH_RISK goto :claq_path_unsafe")
    first_pick = next(i for i, line in enumerate(lines) if line.startswith("call :claq_pick"))
    override = next(i for i, line in enumerate(lines) if line == "if defined CLAQ_PYTHON goto :claq_override")

    # 既定は未設定 → 一致したときだけ立つ → 立ったら分岐、の順であること。
    assert reset < branch < first_pick
    # CLAQ_PYTHON の分岐が PATH 検査より前にあることを **residual として** 固定する。
    # POSIX 側は逆順（PATH 検査が先）で、絶対パスの CLAQ_PYTHON でも回復しない —
    # launcher とその子プロセス（git を含む）は汚染された PATH で解決を続けるため、
    # CLAQ_PYTHON はそれを直さないからである。順序を揃えるのが正しい変更だが、
    # 開発ホストから cmd.exe を実行できず、壊れた並べ替えは Windows で保護を黙って
    # 無効化する。`claq-hook.cmd` の KNOWN RESIDUAL と docs/adr/hook-failure-direction.md を参照。
    assert override < reset

    setters = [line for line in lines[reset + 1 : branch] if line.endswith('set "CLAQ_PATH_RISK=1"')]
    # `;;` の置換比較と、先頭・末尾の substring 検査で 3 本。
    assert len(setters) == 3
    assert all(line.startswith("if ") for line in setters)
    assert any("CLAQ_PATH_PROBE" in line for line in setters)
    assert any('"%PATH:~0,1%|"==";|"' in line for line in setters)
    assert any('"%PATH:~-1%|"==";|"' in line for line in setters)

    # 反復も遅延展開も使わないこと（`!` を含む Windows パスを壊さないため）。
    probe = next(line for line in lines[reset : branch] if line.startswith('set "CLAQ_PATH_PROBE='))
    assert probe == 'set "CLAQ_PATH_PROBE=%PATH:;;=;@;%"'
    assert not any(line.lstrip().lower().startswith("for ") for line in lines[reset:branch])
    assert "EnableDelayedExpansion" not in "\n".join(lines)


def test_windows_wrapper_path_diagnostic_fails_open() -> None:
    """PATH 検査の診断が deny(2) ではなく exit 0 で終わること。

    PreToolUse の 2 は deny なので、ここで非 0 を返すと PATH の直し方
    （Bash / Edit）ごとセッション内から塞がって復旧不能になる。他の
    fail-open 経路と同じ形であることを固定する。
    """
    lines = _cmd_lines()
    label = next(i for i, line in enumerate(lines) if line == ":claq_path_unsafe")
    diag = next(i for i, line in enumerate(lines) if "path_has_empty_entry" in line)

    assert label < diag
    assert "exit /b 0" in lines[diag + 1 : diag + 3]
    # 構造ルール 6: 診断 JSON にパスを埋め込まない。
    assert "%PATH%" not in lines[diag]
    # 回復手段（絶対パスの CLAQ_PYTHON）を人間可読行で案内していること。
    assert any("CLAQ_PYTHON" in line for line in lines[label:diag])


def test_windows_wrapper_requires_an_absolute_claq_python() -> None:
    """CLAQ_PYTHON の絶対パス検査が Windows 側にもあること（B）。

    cmd.exe は裸の名前も `.\\name` も**常に cwd から先に**解決するため、この穴は
    Windows では無条件に成立する。POSIX 側は `CLAQ_PYTHON=./python3` が実測で
    乗っ取りに成立したので、両側とも実在の穴であって対称性のための対称性では
    ない。darwin から cmd.exe を実行できないので、ここは形の検査で固定する。

    受け付ける形は `C:\\...` / `C:/...` / UNC `\\\\...` / `//...` の 4 つ。比較の
    中に `|` の番兵を挟むのは、閉じ引用符の直前でバックスラッシュを終端させない
    ため（構造ルール 3 と同じ落とし穴を検査側にも作らない）。
    """
    lines = _cmd_lines()
    override = next(i for i, line in enumerate(lines) if line == ":claq_override")
    exec_label = next(i for i, line in enumerate(lines) if line == ":claq_exec")
    gates = [line for line in lines if line.startswith('if "%CLAQ_PYTHON:~')]

    assert len(gates) == 4
    accepted = set()
    for gate in gates:
        assert gate.endswith("goto :claq_override_ok")
        assert '|"==' in gate, f"番兵なしの比較: {gate}"
        assert '\\"' not in gate, f"閉じ引用符の直前がバックスラッシュ: {gate}"
        # `if "<左辺>"=="<右辺>" goto ...` の右辺だけを取り出す。
        accepted.add(gate.split("==", 1)[1].split(" ", 1)[0])
    assert accepted == {'":\\|"', '":/|"', '"\\\\|"', '"//|"'}
    # 検査は override 節の中にあり、exec へ到達する前に置かれていること。
    assert all(override < lines.index(gate) < exec_label for gate in gates)
    # 拒否したら PATH 候補へ落とさず、その場で fail-open すること。
    reject = next(i for i, line in enumerate(lines) if "claq_python_not_absolute" in line)
    assert lines[reject + 1] == "exit /b 0"
    assert not any(line.startswith("call :claq_pick") for line in lines[override:])


def test_windows_wrapper_quotes_every_path_expansion() -> None:
    """パスを持ちうる %VAR% が必ず引用符の内側で展開されること（C）。

    cmd はパーセント展開の**後**に行を再パースするので、`echo ... %CLAQ_LAUNCHER%`
    のような裸の展開はインストールパスに `&` があるとコマンド実行になる。
    Windows のアカウント名は `&` を許すため `C:\\Users\\A&B\\...` は攻撃者が
    いなくても成立する。
    """
    for line in _cmd_lines():
        for var in ("%CLAQ_LAUNCHER%", "%CLAQ_PYTHON%", "%CLAQ_PY%", "%CLAQ_ROOT%"):
            start = line.find(var)
            while start != -1:
                # 直前までの `"` が奇数個なら、その展開は引用符の内側にある。
                assert line.count('"', 0, start) % 2 == 1, f"引用符の外で展開している: {line}"
                start = line.find(var, start + 1)


def test_windows_wrapper_probes_each_candidate_with_its_exec_arguments() -> None:
    """採用前に 3.12+ probe を通し、probe と exec の引数が一致すること（H-18b）。

    `py.exe` だけがあり Python 3 が未登録の host では `py -3` が非ゼロで終わる。
    probe が無いとその候補のまま exec し、launcher が一度も動かないまま
    診断も出ず、PATH 上の正常な python3 も試されない。
    probe を `%CLAQ_PYARGS%` 付きで行うのは、bare `py` と `py -3` が別の
    ランタイムを選びうるため（probe した物と exec する物を一致させる）。

    probe は `<nul` で stdin を閉じる（G）。閉じないと probe が hook の
    ツール入力 JSON を食いうる。exec 行には付けない（本体は stdin を読む）。
    """
    lines = _cmd_lines()
    probe = next(line for line in lines if "sys.version_info" in line)

    assert probe.startswith('"%CLAQ_PY%"')
    assert "%CLAQ_PYARGS%" in probe
    assert "(3, 12)" in probe
    assert "<nul" in probe
    # 失敗した候補は捨てられ、次の候補名へ進めること。
    assert 'if errorlevel 1 set "CLAQ_PY="' in lines


def test_windows_wrapper_probes_python3_as_well() -> None:
    """`python3` だけ probe を免除しないこと（E）。

    旧実装は `if /i "%1"=="python3" exit /b 0` で probe を飛ばしていた。候補
    フィルタは `\\WindowsApps` の denylist なので、Microsoft が alias stub の
    設置先を変えるとフィルタを抜けた stub が**無 probe で採用**され、exit 9009
    （非ブロッキングエラー）＝保護無効へ倒れる。POSIX 側の `python3` 無 probe は
    launcher.py が `unsupported_python_version` を出すことに支えられた措置だが、
    Windows の 9009 は launcher へ到達しないので同じ理屈が使えない。
    """
    lines = _cmd_lines()

    assert not any(line.lower().startswith('if /i "%1"=="python3"') for line in lines)
    # probe が `:claq_pick` の唯一の出口条件であること（候補名で分岐しない）。
    pick = lines[lines.index(":claq_pick") :]
    early_exits = [line for line in pick if line.startswith("if not defined CLAQ_PY exit /b")]
    assert early_exits == ["if not defined CLAQ_PY exit /b 0"]


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
        if line.startswith('"%CLAQ_PY%"') and "%CLAQ_LAUNCHER%" in line
    )
    assert lines[exec_index + 1] == "exit /b"


def test_windows_wrapper_launches_python_outside_any_block() -> None:
    """インタプリタ起動行がカッコブロックの中に無いこと。

    ブロック内で起動すると %ERRORLEVEL% の parse 時展開に戻ってしまうため、
    「ブロックを使わない」こと自体を契約として固定する。
    """
    depth = 0
    for line in _cmd_lines():
        if line.startswith('"%CLAQ_PY%"') or line.startswith('"%CLAQ_PYTHON%"'):
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
