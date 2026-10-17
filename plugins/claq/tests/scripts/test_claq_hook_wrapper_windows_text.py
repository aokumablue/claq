"""Windows 版 .cmd と両 wrapper の静的テキスト契約テスト。

Windows 実機では .cmd を実行できないため、POSIX 版 runtime/claq-hook と
同じ解決順・同じ fail-open・同じ診断理由を宣言していることを、wrapper の
テキストを読んで固定する。実際に wrapper を起動して挙動を見る POSIX 側の
テストは test_claq_hook_wrapper.py の担当。
"""

from __future__ import annotations

from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[2]
_WRAPPER = _PLUGIN_ROOT / "runtime" / "claq-hook"
_WRAPPER_CMD = _PLUGIN_ROOT / "runtime" / "claq-hook.cmd"

_PROTECTION_DISABLED_KEY = "claqProtectionDisabled"


def test_launcher_diagnostics_never_embed_a_path_in_json() -> None:
    """診断 JSON にパスを載せないこと。

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
    """launcher 不在/不読の検査がインタプリタ選択より前にあること（Windows 側）。

    POSIX 側だけ塞ぐと、同じ不完全インストールが Windows でだけ exit 2 の
    偽 deny になる。順序が契約の本体なので、宣言の有無ではなく**位置**を見る。
    可読性検査も同じ位置に要る: POSIX 側は `[ -f ]` と `[ -r ]` を対で
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
    ``CLAQ_PATH_RISK`` は未設定のまま残り、解決は通常どおり進む。壊れた検査が
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
    """CLAQ_PYTHON の絶対パス検査が Windows 側にもあること。

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
    """パスを持ちうる %VAR% が必ず引用符の内側で展開されること。

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
    """採用前に 3.12+ probe を通し、probe と exec の引数が一致すること。

    `py.exe` だけがあり Python 3 が未登録の host では `py -3` が非ゼロで終わる。
    probe が無いとその候補のまま exec し、launcher が一度も動かないまま
    診断も出ず、PATH 上の正常な python3 も試されない。
    probe を `%CLAQ_PYARGS%` 付きで行うのは、bare `py` と `py -3` が別の
    ランタイムを選びうるため（probe した物と exec する物を一致させる）。

    probe は `<nul` で stdin を閉じる。閉じないと probe が hook の
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
    """`python3` だけ probe を免除しないこと。

    `if /i "%1"=="python3" exit /b 0` のように probe を飛ばすと危うい。候補
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
    報告され、Windows でだけ全ブロックが素通りする（stdin の読み違いと同じ失敗クラス）。
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
    """`where` の検索範囲を PATH に限定すること。

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
