"""Windows 版 .cmd と両 wrapper の静的テキスト契約テスト。

Windows 実機では .cmd を実行できないため、POSIX 版 runtime/claq-hook と
同じ解決順・同じ fail-open・同じ診断理由を宣言していることを、wrapper の
テキストを読んで固定する。実際に wrapper を起動して挙動を見る POSIX 側の
テストは test_claq_hook_wrapper.py の担当。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_POSIX_TEXT = (_PLUGIN_ROOT / "runtime" / "claq-hook").read_text(encoding="utf-8")
_CMD_TEXT = (_PLUGIN_ROOT / "runtime" / "claq-hook.cmd").read_text(encoding="utf-8")
# Windows wrapper の実行行。`rem` コメント（区切りの裸 `rem` を含む）と空行を落とす。
_LINES = [
    line
    for line in (raw.strip() for raw in _CMD_TEXT.splitlines())
    if line and line.lower() != "rem" and not line.lower().startswith("rem ")
]

_PROTECTION_DISABLED_KEY = "claqProtectionDisabled"


def _at(matches: Callable[[str], bool]) -> int:
    """`matches` が真になる最初の実行行の位置を返す。"""
    return next(i for i, line in enumerate(_LINES) if matches(line))


def test_launcher_diagnostics_never_embed_a_path_in_json() -> None:
    """診断 JSON にパスを載せないこと。

    POSIX のパスは `"`・`\\`・改行を全て含んでよい。生のまま JSON へ連結すると壊れ、改行を含むパスなら
    偽の構造化行を 1 本注入できる。移植可能なエスケープ手段が無い（`${var//}` は bash 専用、`sed` は PATH が
    壊れたこの状況では呼べない）ので、パスは人間可読行だけに載せる。Windows 側も `%CLAQ_LAUNCHER_JSON%`
    ごと落としてある（片側だけ塞がない）。
    """
    posix_json = [line for line in _POSIX_TEXT.splitlines() if _PROTECTION_DISABLED_KEY in line]
    windows_json = [
        line
        for line in _CMD_TEXT.splitlines()
        if _PROTECTION_DISABLED_KEY in line and not line.strip().lower().startswith("rem")
    ]

    assert posix_json and windows_json
    # 検査するのは変数**展開**であって、候補名としての文字列ではない（`"candidates": [...]` は固定文字列）。
    for line in posix_json:
        assert "$_claq_launcher" not in line
        assert "$CLAQ_PYTHON" not in line
        assert "${CLAQ_PYTHON" not in line
    for line in windows_json:
        assert "%CLAQ_LAUNCHER" not in line
        assert "%CLAQ_PYTHON%" not in line
    assert "CLAQ_LAUNCHER_JSON" not in _CMD_TEXT


def test_windows_wrapper_shares_the_posix_contract() -> None:
    """Windows 版 .cmd が POSIX 版と同じ fail-open を宣言し、解決順が CLAUDE.md 規定どおりであること。

    存在検査だけでは `python` と `python3` を入れ替えても緑のままなので、
    `CLAQ_PYTHON` > `py -3` > `python` > `python3` の**順序**を固定する。
    """
    picks = ["call :claq_pick py -3\n", "call :claq_pick python\n", "call :claq_pick python3\n"]
    indexes = [_CMD_TEXT.find(pick) for pick in picks]

    assert "%CLAQ_PYTHON%" in _CMD_TEXT
    assert all(index != -1 for index in indexes), picks
    assert indexes == sorted(indexes), f"インタプリタ解決順が CLAUDE.md 規定と違う: {indexes}"
    assert '"%CLAQ_LAUNCHER%"' in _CMD_TEXT
    assert _PROTECTION_DISABLED_KEY in _CMD_TEXT
    assert "exit /b 0" in _CMD_TEXT


def test_both_wrappers_use_the_same_diagnostic_reasons() -> None:
    """両 OS で共通の失敗理由が同じ文字列で出ること（「片方だけ塞ぐ」再発を止める対称性ガード）。

    PATH 由来の穴は塞ぎ方が違うので理由文字列も違う: POSIX は非絶対要素を除去して続行する
    （`path_not_absolute`）。cmd は除去に遅延展開が要り `!` を含むパスを壊すため、零長要素を検出して
    fail-open する（`path_has_empty_entry`）。残る非対称は .cmd の KNOWN RESIDUAL に明記してある。
    """
    for reason in ("launcher_not_found", "python_not_found", "claq_python_not_absolute"):
        assert reason in _POSIX_TEXT, reason
        assert reason in _CMD_TEXT, reason
    assert "path_not_absolute" in _POSIX_TEXT
    assert "path_has_empty_entry" in _CMD_TEXT
    assert "KNOWN RESIDUAL" in _CMD_TEXT
    assert "UNVERIFIED FROM THIS HOST" in _CMD_TEXT


def test_windows_wrapper_checks_the_launcher_before_choosing_an_interpreter() -> None:
    """launcher の不在・不読検査がインタプリタ選択より前にあり、exit 0 で終わること（Windows 側）。

    POSIX 側だけ塞ぐと、同じ不完全インストールが Windows でだけ exit 2 の偽 deny になる。順序が契約の
    本体なので位置を見る。POSIX 側は `[ -f ]` と `[ -r ]` を対で見ており、`type` を欠くと「存在するが
    読めない launcher」が Windows でだけ偽 deny に戻る。
    """
    guard = _at(lambda line: line.startswith("if not exist"))
    readable = _at(lambda line: line.startswith('type "%CLAQ_LAUNCHER%"'))
    first_pick = _at(lambda line: line.startswith("call :claq_pick"))
    pythonenv = _at(lambda line: line.startswith("if defined CLAQ_PYTHON"))
    diag = _at(lambda line: "launcher_not_found" in line)

    assert guard < readable < pythonenv < first_pick
    assert "%CLAQ_LAUNCHER%" in _LINES[guard]
    assert _LINES[guard].endswith("goto :claq_no_launcher")
    assert _LINES[readable + 1] == "if errorlevel 1 goto :claq_no_launcher"
    # 診断行より後ろに素の exit /b 0 があり、deny(2) に化けないこと。
    assert "exit /b 0" in _LINES[diag + 1 :]


def test_windows_wrapper_detects_empty_path_entries() -> None:
    """PATH の空要素を検出して fail-open へ倒す分岐があること（形の検査）。

    cmd.exe は零長の PATH 要素（``;;``・先頭 ``;``・末尾 ``;``）を現在ディレクトリとして解決し、
    フックは cwd=プロジェクトルートで動くので、``where "$PATH:..."`` を使っていても cwd の python.exe に届く。
    darwin から cmd.exe を実行できないので、固定するのは「既定は未設定 → 一致したときだけ立つ →
    立ったら ``:claq_path_unsafe`` へ飛ぶ」構造だけ。検査行が壊れても ``CLAQ_PATH_RISK`` は未設定のまま
    残り、壊れた検査が「Windows で保護が無効」へ倒れることはない。
    """
    reset = _LINES.index('set "CLAQ_PATH_RISK="')
    branch = _LINES.index("if defined CLAQ_PATH_RISK goto :claq_path_unsafe")
    first_pick = _at(lambda line: line.startswith("call :claq_pick"))
    override = _LINES.index("if defined CLAQ_PYTHON goto :claq_override")

    assert reset < branch < first_pick
    # CLAQ_PYTHON の分岐が PATH 検査より前にあることを **residual として** 固定する。POSIX 側は逆順で、
    # 順序を揃えるのが正しい変更だが、cmd.exe を実行できない開発ホストでの並べ替えは Windows で保護を
    # 黙って無効化しうる（`claq-hook.cmd` の KNOWN RESIDUAL と docs/adr/hook-failure-direction.md）。
    assert override < reset

    section = _LINES[reset:branch]
    setters = [line for line in section[1:] if line.endswith('set "CLAQ_PATH_RISK=1"')]
    # `;;` の置換比較と、先頭・末尾の substring 検査で 3 本。
    assert len(setters) == 3
    assert all(line.startswith("if ") for line in setters)
    assert any("CLAQ_PATH_PROBE" in line for line in setters)
    assert any('"%PATH:~0,1%|"==";|"' in line for line in setters)
    assert any('"%PATH:~-1%|"==";|"' in line for line in setters)
    # 反復も遅延展開も使わないこと（`!` を含む Windows パスを壊さないため）。
    assert next(line for line in section if line.startswith('set "CLAQ_PATH_PROBE=')) == (
        'set "CLAQ_PATH_PROBE=%PATH:;;=;@;%"'
    )
    assert not any(line.lstrip().lower().startswith("for ") for line in section)
    assert "EnableDelayedExpansion" not in "\n".join(_LINES)


def test_windows_wrapper_path_diagnostic_fails_open() -> None:
    """PATH 検査の診断が deny(2) ではなく exit 0 で終わり、診断 JSON にパスを埋め込まないこと。

    PreToolUse の 2 は deny なので、非 0 だと PATH の直し方（Bash / Edit）ごと塞がって復旧不能になる。
    """
    label = _LINES.index(":claq_path_unsafe")
    diag = _at(lambda line: "path_has_empty_entry" in line)

    assert label < diag
    assert "exit /b 0" in _LINES[diag + 1 : diag + 3]
    assert "%PATH%" not in _LINES[diag]
    # 回復手段（絶対パスの CLAQ_PYTHON）を人間可読行で案内していること。
    assert any("CLAQ_PYTHON" in line for line in _LINES[label:diag])


def test_windows_wrapper_requires_an_absolute_claq_python() -> None:
    """CLAQ_PYTHON の絶対パス検査が Windows 側にもあること（形の検査）。

    cmd.exe は裸の名前も `.\\name` も常に cwd から先に解決するので、この穴は Windows では無条件に成立する。
    受け付ける形は `C:\\...` / `C:/...` / UNC `\\\\...` / `//...` の 4 つ。比較に `|` の番兵を挟むのは、
    閉じ引用符の直前でバックスラッシュを終端させないため。
    """
    override = _LINES.index(":claq_override")
    exec_label = _LINES.index(":claq_exec")
    gates = [line for line in _LINES if line.startswith('if "%CLAQ_PYTHON:~')]
    reject = _at(lambda line: "claq_python_not_absolute" in line)

    assert len(gates) == 4
    for gate in gates:
        assert gate.endswith("goto :claq_override_ok")
        assert '|"==' in gate, f"番兵なしの比較: {gate}"
        assert '\\"' not in gate, f"閉じ引用符の直前がバックスラッシュ: {gate}"
        assert override < _LINES.index(gate) < exec_label
    # `if "<左辺>"=="<右辺>" goto ...` の右辺だけを取り出す。
    assert {gate.split("==", 1)[1].split(" ", 1)[0] for gate in gates} == {'":\\|"', '":/|"', '"\\\\|"', '"//|"'}
    # 拒否したら PATH 候補へ落とさず、その場で fail-open すること。
    assert _LINES[reject + 1] == "exit /b 0"
    assert not any(line.startswith("call :claq_pick") for line in _LINES[override:])


def test_windows_wrapper_quotes_every_path_expansion() -> None:
    """パスを持ちうる %VAR% が必ず引用符の内側で展開されること。

    cmd はパーセント展開の後に行を再パースするので、裸の展開はインストールパスに `&` があると
    コマンド実行になる（Windows のアカウント名は `&` を許す）。
    """
    for line in _LINES:
        for var in ("%CLAQ_LAUNCHER%", "%CLAQ_PYTHON%", "%CLAQ_PY%", "%CLAQ_ROOT%"):
            start = line.find(var)
            while start != -1:
                # 直前までの `"` が奇数個なら、その展開は引用符の内側にある。
                assert line.count('"', 0, start) % 2 == 1, f"引用符の外で展開している: {line}"
                start = line.find(var, start + 1)


def test_windows_wrapper_probes_each_candidate_with_its_exec_arguments() -> None:
    """採用前に 3.12+ probe を `%CLAQ_PYARGS%` 付き・stdin を閉じて通し、失敗した候補は捨てること。

    `py.exe` だけで Python 3 未登録の host では `py -3` が非 0 で終わり、probe が無いと launcher が
    一度も動かないまま診断も出ない。bare `py` と `py -3` は別ランタイムを選びうるので probe と exec の
    引数を揃える。`<nul` が無いと probe が hook のツール入力 JSON を食いうる。
    """
    probe = _LINES[_at(lambda line: "sys.version_info" in line)]

    assert probe.startswith('"%CLAQ_PY%"')
    assert "%CLAQ_PYARGS%" in probe
    assert "(3, 12)" in probe
    assert "<nul" in probe
    assert 'if errorlevel 1 set "CLAQ_PY="' in _LINES


def test_windows_wrapper_probes_python3_as_well() -> None:
    """`python3` だけ probe を免除しないこと。

    候補フィルタは `\\WindowsApps` の denylist なので、alias stub の設置先が変わるとフィルタを抜けた stub が
    無 probe で採用され、exit 9009 ＝保護無効へ倒れる。POSIX 側の `python3` 無 probe は launcher.py の
    `unsupported_python_version` に支えられた措置だが、9009 は launcher へ到達しないので同じ理屈が使えない。
    """
    assert not any(line.lower().startswith('if /i "%1"=="python3"') for line in _LINES)
    # probe が `:claq_pick` の唯一の出口条件であること（候補名で分岐しない）。
    pick = _LINES[_LINES.index(":claq_pick") :]
    assert [line for line in pick if line.startswith("if not defined CLAQ_PY exit /b")] == [
        "if not defined CLAQ_PY exit /b 0"
    ]


def test_windows_wrapper_never_expands_errorlevel_after_running_python() -> None:
    """`%ERRORLEVEL%` を展開せず、launcher 起動行の直後が素の `exit /b` であること。

    cmd は `if (...)` ブロック内の `%VAR%` をパース時に展開するので、`exit /b %ERRORLEVEL%` は起動前の値を
    返し、保護 hook の exit 2 が 0 として報告される。`if errorlevel N` は実行時比較なので対象外。
    コメントは対象外（この落とし穴の説明そのものが本文に書いてある）。
    """
    exec_index = _at(lambda line: line.startswith('"%CLAQ_PY%"') and "%CLAQ_LAUNCHER%" in line)

    assert not any("%ERRORLEVEL%" in line.upper() for line in _LINES)
    assert _LINES[exec_index + 1] == "exit /b"


def test_windows_wrapper_launches_python_outside_any_block() -> None:
    """インタプリタ起動行がカッコブロックの中に無いこと（%ERRORLEVEL% の parse 時展開に戻さない）。"""
    depth = 0
    for line in _LINES:
        if line.startswith(('"%CLAQ_PY%"', '"%CLAQ_PYTHON%"')):
            assert depth == 0, f"インタプリタ起動がブロック内にある: {line}"
        depth += line.count("(") - line.count(")")


def test_windows_wrapper_where_pipeline_searches_path_only_and_filters_store_alias() -> None:
    """`where` の検索範囲を PATH に限定し、WindowsApps 除外を `where` のパイプライン側で行うこと。

    `where` は仕様上 PATH より先にカレントディレクトリを見るので、リポジトリ直下の `python.exe` が hook の
    stdin ごと採用される（POSIX の `command -v` は cwd を見ない）。for 変数は展開後に再パースされるため、
    `echo %%P| findstr ...` だと `C:\\Program Files (x86)\\...` でブロックが壊れる。findstr のパターンが
    閉じ引用符直前でバックスラッシュを終端すると、`\\"` がエスケープと読まれて永久に一致しなくなる。
    """
    pick = _LINES[_at(lambda line: line.startswith("for /f") and "where " in line)]

    assert 'where "$PATH:%1.exe"' in pick
    assert "where %1" not in pick
    assert "^| findstr /i /v /c:" in pick
    assert '"\\WindowsApps"' in pick
    assert '\\"' not in pick
    assert "echo" not in pick
