"""runtime/claq-hook（POSIX 版）の起動契約テスト。

hooks.json の全エントリはこの wrapper 経由で launcher を起動する。裸の
``python3`` を hooks.json へ書くと、Windows で ``python3`` が
Microsoft Store の App execution alias に解決され、launcher へ到達する前に
全 PreToolUse が hook error になる。
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

Windows 版 .cmd と両 wrapper の静的テキスト契約は
test_claq_hook_wrapper_windows_text.py の担当。
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
    """launcher が無いとき exit 2 ではなく exit 0 + 診断で終わること。

    launcher の存在を確認せず `exec python3 "$_claq_launcher"` すると、
    `python3 <存在しないファイル>` は **exit 2** を返し、PreToolUse の
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
    """`$0` にスラッシュが無い起動でも偽 deny にならないこと。

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

    片方だけを stub するテストでは、実装の順序を入れ替えても緑のまま通る。
    順序そのものを踏む対照としてここで固定する。
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
    """3.12+ probe が hook の stdin を食わないこと。

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


# --- cwd 由来インタプリタの採用（実測で悪用可能な経路） -------------------------
#
# POSIX は PATH の長さ 0 の要素（先頭 `:`・末尾 `:`・連続 `::`）と `.` を
# 「カレントディレクトリ」と規定する。hook は cwd=プロジェクトルートで起動する
# ため、リポジトリが同梱した `./python3` が `command -v python3` と
# `exec python3` の両方で採用され、hook の stdin（ツール入力 JSON）を受け取り
# exit code まで支配できる（= 保護の完全な乗っ取り。実測で再現する）。
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


# --- CLAQ_PYTHON の相対値 ------------------------------------------------------
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

    `CLAQ_PYTHON=./python3` は POSIX 側でも実測で乗っ取りが成立する。裸の名前も
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
