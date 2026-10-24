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
_HOOK = "claq.hooks.pre_compact"

_PROTECTION_DISABLED_KEY = "claqProtectionDisabled"

# cwd 由来の偽インタプリタが動いたことだけを示す印。実際の攻撃はここで hook の
# stdin（ツール入力 JSON）を読み、exit code も自由に決められる。
_HIJACK_MARKER = "CLAQ-HIJACKED"
_HIJACK_BODY = f'printf "{_HIJACK_MARKER}\\n"\nexit 0'


def _stub_dir(parent: Path, name: str, **stubs: str) -> Path:
    """`parent/name` を作り、各キー名の実行可能シェルスタブ（値はシェバン以降の本文）を置く。"""
    directory = parent / name
    directory.mkdir(parents=True, exist_ok=True)
    for stub, body in stubs.items():
        (directory / stub).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        (directory / stub).chmod(0o755)
    return directory


def _env(home: Path, path: str, **extra: str) -> dict[str, str]:
    """PATH を生の文字列で差し替えた最小環境を組み立てる（空要素や `.` もそのまま渡せる）。"""
    return {"PATH": path, "HOME": str(home), "LANG": "C", **extra}


def _run_wrapper(
    env: dict[str, str], *, cmd: list[str] | None = None, cwd: Path | None = None, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    """wrapper（既定はリポジトリ内の実体で pre_compact を起動）を隔離環境で実行する。"""
    return subprocess.run(
        cmd or [str(_WRAPPER), _HOOK],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
        timeout=60,
        check=False,
    )


def _diagnostic(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    """stderr 末尾の構造化診断行を読む。"""
    return json.loads(result.stderr.strip().splitlines()[-1])


def _hijack_tree(tmp_path: Path) -> tuple[Path, Path]:
    """偽 python3/python 入りの「悪意あるリポジトリ」と、PATH 上の唯一の絶対ディレクトリ（空）を用意する。

    PATH 側を空にしないと `/usr/bin/python3` のような実在候補が順序で勝ち、修正が無くても
    テストが緑になる（実測でその取り違えを確認済み）。
    """
    repo = _stub_dir(tmp_path, "hostile-repo", python3=_HIJACK_BODY, python=_HIJACK_BODY)
    return repo, _stub_dir(tmp_path, "empty-bin")


def test_wrapper_is_executable() -> None:
    """wrapper に実行ビットが立っていること（配布物でも exec できる前提）。"""
    assert os.access(_WRAPPER, os.X_OK)


@pytest.mark.parametrize("variant", ["missing", "unreadable", "bare_dollar0"])
def test_wrapper_fails_open_when_launcher_is_unusable(tmp_path: Path, variant: str) -> None:
    """launcher が不在・不読・cwd 依存の root 解決で見つからないとき、exit 2 ではなく exit 0 + 診断で終わること。

    `python3 <存在しない/読めないファイル>` は **exit 2** を返し、PreToolUse の「exit 2 = deny」契約で
    4 つの保護 hook が全ツール呼び出しを偽 deny する。復旧手段の Bash ごと塞がるのでセッション内から
    直せない。不完全インストール・Grok の hash symlink 切れと同じく `<root>/src` を作らない。

    - unreadable: 存在検査だけで可読性検査を落とすと、パーミッション事故が偽 deny に化ける。
    - bare_dollar0: `sh claq-hook` だと `$0` にスラッシュが無く、`*) _claq_hook_dir=.` 分岐が root を
      `$PWD/..` として解決する（PATH 経由の起動では到達しない）。人間可読行に実際に探したパスが載る。

    `CLAQ_PYTHON` に実在するインタプリタを渡すのが要点。使えない PATH だと後段の分岐でも exit 0 になり、
    launcher 検査がインタプリタ選択より前にあることを証明できない。
    """
    wrapper_dir = tmp_path / ("bare" if variant == "bare_dollar0" else "runtime")
    wrapper_dir.mkdir()
    copy = wrapper_dir / _WRAPPER.name
    copy.write_bytes(_WRAPPER.read_bytes())
    copy.chmod(0o755)
    launcher = tmp_path / "src" / "claq" / "launcher.py"
    if variant == "unreadable":
        launcher.parent.mkdir(parents=True)
        launcher.write_text("raise SystemExit(0)\n", encoding="utf-8")
        launcher.chmod(0o000)
    # PATH には空の絶対ディレクトリだけを置く（ambient な python3 を拾わせない）。
    env = _env(tmp_path, str(_stub_dir(tmp_path, "empty-bin")), CLAQ_PYTHON=sys.executable)
    if variant == "bare_dollar0":
        result = _run_wrapper(env, cmd=["/bin/sh", copy.name, _HOOK], cwd=wrapper_dir)
    else:
        result = _run_wrapper(env, cmd=[str(copy), _HOOK])

    assert result.returncode == 0, f"deny に化けている: {result.returncode} / {result.stderr}"
    assert result.stdout == ""
    assert _diagnostic(result) == {_PROTECTION_DISABLED_KEY: True, "reason": "launcher_not_found"}
    # 探したパスは人間可読行だけに載る（JSON へ生連結すると壊れる）。
    assert str(launcher) in result.stderr.splitlines()[0]


def test_wrapper_uses_claq_python_override(tmp_path: Path) -> None:
    """CLAQ_PYTHON（絶対パス）が最優先で使われ、launcher と対象モジュールまで到達する。"""
    env = _env(
        tmp_path,
        str(_stub_dir(tmp_path, "empty-bin")),
        CLAQ_PYTHON=sys.executable,
        CLAQ_LEARN_KIND="fact",
        CLAQ_LEARN_TITLE="wrapper reached the module",
    )

    result = _run_wrapper(env, cmd=[str(_WRAPPER), "claq.mem.learn_payload"])

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"kind": "fact", "title": "wrapper reached the module"}


@pytest.mark.parametrize("with_python", [False, True], ids=["python3-only", "python-too"])
def test_wrapper_prefers_python3_on_path(tmp_path: Path, with_python: bool) -> None:
    """CLAQ_PYTHON が無ければ PATH 上の python3 を版の検査なしで使い、python が同居しても python3 が勝つこと。

    片方だけを stub する行では順序を入れ替えても緑のままなので、両方置く行で CLAUDE.md 規定の順序を踏む。
    PATH 洗浄が「PATH を空にするだけ」に退化していないことの陽性対照でもある（ここが赤なら下の乗っ取り
    テスト群が緑でも意味がない）。
    """
    stubs = {"python3": 'printf "python3:%s\\n" "$1"\nexit 0'}
    if with_python:
        stubs["python"] = 'printf "PY-WON\\n"\nexit 0'

    result = _run_wrapper(_env(tmp_path, str(_stub_dir(tmp_path, "bin", **stubs))))

    assert result.returncode == 0
    assert "PY-WON" not in result.stdout
    assert result.stdout.startswith("python3:")
    assert result.stdout.strip().endswith("src/claq/launcher.py")


def test_wrapper_falls_back_to_python_when_recent_enough(tmp_path: Path) -> None:
    """python3 が無く python が 3.12+ なら python で起動する。"""
    bin_dir = _stub_dir(tmp_path, "bin", python='if [ "$1" = "-c" ]; then exit 0; fi\nprintf "python:%s\\n" "$1"')

    result = _run_wrapper(_env(tmp_path, str(bin_dir)))

    assert result.returncode == 0
    assert result.stdout.startswith("python:")


@pytest.mark.parametrize(
    "stubs",
    [{"python": 'if [ "$1" = "-c" ]; then exit 1; fi\nprintf "python:%s\\n" "$1"'}, {}],
    ids=["python-older-than-312", "no-interpreter"],
)
def test_wrapper_reports_python_not_found(tmp_path: Path, stubs: dict[str, str]) -> None:
    """使えるインタプリタが無ければ起動せず exit 0 + 構造化診断（stdout は汚さない）。

    fail-closed（非 0）にすると保護 hook が全 Bash/Edit を拒否し、PATH を直す手段ごとセッション内から
    塞がれる。launcher.py の未対応 Python 時と同じ fail-open ポリシーに揃える（CLAUDE.md「ランタイム前提」）。
    """
    result = _run_wrapper(_env(tmp_path, str(_stub_dir(tmp_path, "bin", **stubs))))

    assert result.returncode == 0
    assert result.stdout == ""
    diagnostic = _diagnostic(result)
    assert diagnostic[_PROTECTION_DISABLED_KEY] is True
    assert diagnostic["reason"] == "python_not_found"
    assert diagnostic["requiredVersion"] == "3.12+"


def test_wrapper_probe_does_not_consume_the_hook_payload(tmp_path: Path) -> None:
    """3.12+ probe が hook の stdin を食わないこと。

    probe を stdin を閉じずに起動すると、コード側が読みさえすればツール入力 JSON がそこで消費され、
    本体は空の payload を読む（保護 hook は検査対象を失う）。`python -c` が今は読まないから安全、
    というのは実装依存なので、`</dev/null` で構造として保証していることを実測で固定する。
    """
    bin_dir = _stub_dir(
        tmp_path,
        "bin",
        python='if [ "$1" = "-c" ]; then cat >/dev/null; exit 0; fi\nprintf "payload=[%s]\\n" "$(cat)"',
    )
    # スタブは `cat` を呼ぶので実在の /bin も足す。洗浄がフィルタであって置き換えではないことの確認も兼ねる。
    result = _run_wrapper(_env(tmp_path, f"{bin_dir}:/bin"), stdin='{"tool_name":"Bash"}')

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'payload=[{"tool_name":"Bash"}]'


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
def test_wrapper_never_adopts_a_cwd_interpreter(tmp_path: Path, label: str, path_template: str) -> None:
    """cwd の `python3` が PATH の空要素・`.`・相対エントリ経由で採用されないこと。"""
    repo, empty = _hijack_tree(tmp_path)

    result = _run_wrapper(_env(tmp_path, path_template.format(empty=empty)), cwd=repo)

    assert _HIJACK_MARKER not in result.stdout, f"{label}: cwd のインタプリタが動いた"
    assert _HIJACK_MARKER not in result.stderr, f"{label}: cwd のインタプリタが動いた"
    assert result.returncode == 0, f"{label}: fail-open していない"
    assert _diagnostic(result)["reason"] == "python_not_found", label


def test_wrapper_refuses_a_path_without_any_absolute_entry(tmp_path: Path) -> None:
    """絶対エントリが 1 つも無い PATH では、CLAQ_PYTHON が絶対パスでも hook を起動しないこと。

    洗浄後に何も残らない PATH で hook を動かすと、hook 自身の `subprocess.run(["git", ...])` が
    cwd の `./git` に解決される（execvp も空要素を cwd として扱う）。攻撃者が得るのが「保護のバイパス」
    ではなく「hook 権限での任意コード実行」になるので、明確に軽い前者へ倒す。
    """
    repo, _ = _hijack_tree(tmp_path)

    result = _run_wrapper(_env(tmp_path, ".:relative/bin:", CLAQ_PYTHON=sys.executable), cwd=repo)

    assert result.returncode == 0
    assert _HIJACK_MARKER not in result.stdout
    assert _diagnostic(result) == {_PROTECTION_DISABLED_KEY: True, "reason": "path_not_absolute"}


def test_wrapper_exports_a_sanitized_path_to_the_interpreter(tmp_path: Path) -> None:
    """洗浄した PATH が子プロセスへ export されること。

    元の PATH を渡し直すと、`./python3` を塞いだ端から hook 本体の `git` 解決が `./git` に落ちて
    同じ乗っ取りが成立する（hook_common.py と commit_quality_scanner.py はどちらも名前で git を起動する）。
    wrapper の責務を「信頼できる PATH の下で解決する」へ意図的に広げた決定を固定する。
    """
    bin_dir = _stub_dir(tmp_path, "bin", python3='printf "PATH=[%s]\\n" "$PATH"\nexit 0')

    result = _run_wrapper(_env(tmp_path, f":{bin_dir}:.:relative/bin:"), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"PATH=[{bin_dir}]"


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
    """CLAQ_PYTHON が相対なら cwd のインタプリタを起動せず、PATH 探索へも落とさないこと。

    `CLAQ_PYTHON=./python3` は POSIX 側でも実測で乗っ取りが成立する。裸の名前も PATH に空要素があれば
    同じ穴になる。明示的な override が黙って別のインタプリタに化けるのは利用者の前提を破る。
    """
    repo, empty = _hijack_tree(tmp_path)
    _stub_dir(repo, "bin", python3=_HIJACK_BODY)
    # PATH 側には正規の python3 を置く。「PATH へ落ちて別物が動く」ことも防ぐ。
    _stub_dir(tmp_path, "empty-bin", python3='printf "FELL-THROUGH-TO-PATH\\n"\nexit 0')

    result = _run_wrapper(_env(tmp_path, str(empty), CLAQ_PYTHON=value), cwd=repo)

    assert _HIJACK_MARKER not in result.stdout, f"{label}: cwd のインタプリタが動いた"
    assert "FELL-THROUGH-TO-PATH" not in result.stdout, f"{label}: PATH 探索へ落ちた"
    assert result.returncode == 0, label
    assert _diagnostic(result)["reason"] == "claq_python_not_absolute", label
