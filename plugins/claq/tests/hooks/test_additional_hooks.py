"""claq.hooks.block_no_verify（git フックのバイパス検出）と pre_compact のテスト、各フックの __main__ 起動のテスト。"""

from __future__ import annotations

import io
import json
import runpy
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from claq.hooks import block_no_verify, pre_compact

# ``--no-verify``。テストケース表の横幅を抑えるための別名。
NV = "--no-verify"

# ブロックされなければならないコマンド。
_BYPASS_COMMANDS = [
    # 素の形
    f"git commit {NV}",
    f"git push {NV}",
    "git commit -n",
    f'git commit {NV} -m "fix: x"',
    f"git commit {NV} -m 'wip'",
    # git グローバルオプションを挟むバイパス（実際に突破された形）
    f"git --no-pager commit {NV}",
    f"git -C /tmp/repo commit {NV}",
    f"git -c core.hooksPath=/dev/null commit {NV}",
    f"git --git-dir /x --work-tree /y commit {NV}",
    f"git --git-dir=/x commit {NV}",
    f"git --exec-path /x commit {NV}",
    # segment 先頭が git 以外のバイパス（環境変数プレフィックス・ラッパー・パス付き）
    f"GIT_DIR=.git git commit {NV}",
    f"FOO=bar git commit {NV}",
    f"env git commit {NV}",
    f"sudo git commit {NV}",
    f"nice git commit {NV}",
    f"command git commit {NV}",
    f"/usr/bin/git commit {NV}",
    f"xargs -I% git commit {NV}",
    f"timeout 5 git commit {NV}",
    # 1 segment 内に git トークンが複数（先頭側はユーザー名 / 別コマンドの引数）
    "sudo -u git git commit -n",
    "git submodule foreach git commit -n",
    # 改行区切り（shlex では区切りトークンにならず 1 segment になる）
    "git add -A\ngit commit -n",
    # 複合コマンド（空白あり / なし）
    f"git add -A && git --no-pager commit {NV}",
    f"git add -A&&git commit {NV}",
    "git add -A; git commit -n -m x",
    f"git status | grep x || git commit {NV}",
    f"&& git commit {NV}",
    # 短オプションのクラスタ
    "git commit -an -m x",
    "git commit -u -n",
    "git commit -F - -n",
    # core.hooksPath オーバーライド（--no-verify を使わずに git 自身の
    # フックを無効化できるため、単独でもブロックする）
    "git -c core.hooksPath=/dev/null commit",
    "git -ccore.hooksPath=/dev/null commit",
    "git -c CORE.HOOKSPATH=/dev/null commit",
    "git --config-env=core.hooksPath=MYVAR commit",
    "git --config-env core.hooksPath=MYVAR commit",
    # sh -c ラッパー 1 段
    f"sh -c 'git commit {NV}'",
    f"bash -c 'git commit {NV}'",
    # -c より前に他オプションが挟まっても -c を探し続ける
    f"sh -x -c 'git commit {NV}'",
    # 未知のグローバルオプションで subcommand 解決がずれる形は fail-closed
    f"git --future-option value commit {NV}",
    "git --future-option value commit -n",
    "git -Z value commit -n",
    # shlex 失敗（未閉じクォート）は fail-closed
    f'git commit -m "unclosed {NV}',
    # 実行ファイル表記ゆれ: macOS 既定 APFS は大小文字を区別しないため
    # `GIT` は実 git を起動する。Windows の `.exe` サフィックスも同一視する。
    f"GIT commit {NV}",
    f"Git commit {NV}",
    f"git.exe commit {NV}",
    f"GIT.EXE commit {NV}",
    "GIT commit -n",
    # literal GIT_CONFIG_* 環境変数プレフィックス: --no-verify を
    # 使わずに core.hooksPath を上書きし git 自身のフックを無効化する。
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp/x git commit -m x",
    "GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "GIT_CONFIG_KEY_0=CORE.HOOKSPATH git commit -m x",
    "env GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -i GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -u FOO GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env -uFOO GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "env --ignore-environment GIT_CONFIG_KEY_0=core.hooksPath git commit -m x",
    "GIT_CONFIG_PARAMETERS=x git commit -m x",
    # `git config` サブコマンド経由の core.hooksPath 書込み・削除。
    "git config core.hooksPath /tmp/evil-hooks",
    "git config CORE.HOOKSPATH /tmp/evil-hooks",
    "git config --unset core.hooksPath",
    "git config --unset-all core.hooksPath",
    "git config set core.hooksPath /tmp/evil-hooks",
    "git config unset core.hooksPath",
    "git config add core.hooksPath /tmp/evil-hooks",
    # git が受理する long オプションの短縮形。完全一致だけを見ると素通りする。
    "git commit --no-veri -m x",
    "git commit --no-ver -m x",
    "git commit --no- -m x",
    "git commit --n -m x",
    # config ファイルそのものを差し替える literal 環境変数。差し替え先で
    # core.hooksPath を書けるため -c core.hooksPath= と等価。
    "GIT_CONFIG_GLOBAL=/tmp/cfg git commit -m x",
    "GIT_CONFIG_SYSTEM=/tmp/cfg git commit -m x",
    "GIT_CONFIG_NOSYSTEM=1 git commit -m x",
    "env GIT_CONFIG_GLOBAL=/tmp/cfg git commit -m x",
    # git は global 設定を $HOME/.gitconfig と $XDG_CONFIG_HOME/git/config から読むため、
    # 前置代入でホームを差し替えると保護対象外のパスの設定が効く。GIT_CONFIG_GLOBAL と同種。
    "HOME=/tmp/h git commit -m x",
    "XDG_CONFIG_HOME=/tmp/h git commit -m x",
    "HOME=/tmp/h git status",
    # `+=` も代入。追記した値でも HOME の差し替えになる。
    "HOME+=x git commit -m x",
    "GIT_CONFIG_GLOBAL+=/tmp/cfg git commit -m x",
    # sh -c の内側が git を起動するなら、外側セグメントの前置代入が内側へ効く。
    "HOME=/tmp/h sh -c 'git commit -m x'",
    "GIT_CONFIG_GLOBAL=/tmp/h/c sh -c 'git commit -m x'",
    "XDG_CONFIG_HOME=/tmp/h bash -lc 'git commit -m x'",
    "env HOME=/tmp/h sh -c 'git commit -m x'",
    "GIT_CONFIG_KEY_0=core.hooksPath sh -c 'git commit -m x'",
    # include.path / includeIf.* は任意 config を取り込める。
    "git -c include.path=/tmp/evil commit -m x",
    "git -c includeIf.gitdir:/x/.path=/tmp/evil commit -m x",
    "git --config-env=include.path=MYVAR commit",
    "git config --global include.path /tmp/evil",
    "GIT_CONFIG_KEY_0=include.path git commit -m x",
    # alias.* の展開先は解釈できないため定義自体を deny する。
    "git -c alias.ci='commit --no-verify' ci -m x",
    "git -c ALIAS.CI='commit -n' ci",
    "git config alias.ci 'commit --no-verify'",
    # 解析不能な入力（`$'\''` で shlex が失敗する）は、生文字列に --no-verify / hooksPath が見えたらブロックする。
    "echo $'\\'';\"git\" commit --no-verify",
    "echo $'\\'';git commit \"--no-verify\"",
    "echo $'\\'';git -c \"core.HooksPath=/x\" commit",
]

# 通さなければならないコマンド（誤検知の回帰防止）。
_ALLOWED_COMMANDS = [
    # -n が --dry-run / 件数指定であるサブコマンド
    "git push -n",
    "git log -n 5",
    "git push --dry-run",
    "git clean -n",
    "git bisect run ./t.sh -n",
    # 既知の boolean グローバルオプションは subcommand 解決を乱さない
    "git --no-pager log -n 5",
    "git -p log -n 5",
    "git -c foo.bar=1 log -n 5",
    # クォート内の -n / --no-verify はフラグではない
    'git commit -m "use -n flag"',
    'git commit -m "docs: explain -n flag usage"',
    "git commit -m'msg with -n'",
    f"git commit -m 'note: {NV} is banned'",
    f'echo "git commit {NV}"',
    'grep -r "git commit" .',
    # 短縮クラスタの既存挙動
    "git commit -mmsg",
    'git commit -am "x"',
    "git commit -uno -m x",
    "git commit --amend --no-edit",
    "git commit -m x -- -n",
    # git 起動トークンより前の -n は git のフラグではない
    "grep -rn git .",
    "ls -n /usr/bin/git",
    "xargs -n 2 git status",
    # core.hooksPath 以外の -c/--config-env は対象外
    "git -c user.name=x commit -m y",
    # sh -c の 2 段以上のネストは非目標（1 段のみ再帰）
    f"sh -c \"sh -c 'git commit {NV}'\"",
    # -c を伴わないシェル起動はラッパー対象外
    "sh script.sh",
    # その他
    "git status",
    "git rebase --continue",
    f"gitk {NV}",
    "",
    "git",
    # literal 環境変数プレフィックスがあっても core.hooksPath と無関係なら allow。
    "FOO=bar git commit -m x",
    "env FOO git commit -m x",
    # HOME の代入は git 起動に前置されたときだけ対象。git を呼ばない前置・別セグメントは allow。
    "HOME=/tmp/h ls",
    "HOME=/tmp/h; git status",
    "XDG_CONFIG_HOME=/tmp/h ls",
    "FOO+=x git commit -m x",
    # 外側の代入でも、内側が git を起動しなければ対象外。内側の代入が外側を上書きする形も allow。
    "HOME=/tmp/h sh -c 'ls'",
    "FOO=1 sh -c 'git commit -m x'",
    "GIT_CONFIG_KEY_0=core.hooksPath sh -c 'GIT_CONFIG_KEY_0=user.name git commit -m x'",
    # 代入は -c の文字列より後ろの位置引数（$0 以降）に置かれても外側の前置代入ではない。
    "sh -c 'git commit -m x' HOME=/tmp/h",
    "env GIT_CONFIG_KEY_0=user.name git commit -m x",
    # `git config` の read-only 操作・無関係キーは allow。
    "git config --get core.hooksPath",
    "git config --get-all core.hooksPath",
    "git config --get-regexp core.hooksPath",
    "git config --show-origin --get core.hooksPath",
    "git config --list",
    "git config core.hooksPath",
    "git config get core.hooksPath",
    "git config list",
    "git config get user.name",
    # 短縮形と紛らわしいが --no-verify の前置ではない commit オプション。
    "git commit --no-edit",
    "git commit --no-gpg-sign -m x",
    "git commit --no-post-rewrite -m x",
    f"git commit -m 'typo: {NV[:-2]} was meant'",
    # 機微でない config key の read-only 操作は allow。
    "git config --get alias.ci",
    "git config alias.ci",
    "git config get include.path",
    "git config user.name x",
    # 危険語が無い解析不能な入力と、解析できた引用文の危険語は巻き込まない。
    "echo $'\\'' && cat ruff.toml",
    "echo $'\\'';git commit -m \"x\"",
    "grep -- '--no-verify' README.md",
]

_BYPASS_REASON = "[Hook] BLOCKED: git hook bypass flags are not allowed"


def _run_entrypoint(module_name: str) -> int:
    """モジュールを __main__ として実行し、終了コードを返す。"""
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module(module_name, run_name="__main__")

    return excinfo.value.code


def _run_module(payload: str) -> subprocess.CompletedProcess[str]:
    """``payload`` を実 stdin へ流して ``python -m claq.hooks.block_no_verify`` を別プロセスで実行する。"""
    src_root = Path(block_no_verify.__file__).resolve().parents[2]
    return subprocess.run(
        [sys.executable, "-m", "claq.hooks.block_no_verify"],
        input=payload,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(src_root), "CLAUDECODE": "1"},
    )


@pytest.mark.parametrize(
    ("command", "expected"),
    [(command, True) for command in _BYPASS_COMMANDS] + [(command, False) for command in _ALLOWED_COMMANDS],
)
def test_has_bypass_flag(command: str, expected: bool) -> None:
    """ラッパー・パス付き起動・グローバルオプション・環境変数・設定経由のバイパスを検出し、バイパスでないコマンドは誤検知しないこと。"""
    assert block_no_verify.has_bypass_flag(command) is expected


@pytest.mark.parametrize(
    ("command", "expected_code"),
    [(command, 2) for command in _BYPASS_COMMANDS] + [(command, 0) for command in _ALLOWED_COMMANDS],
)
def test_end_to_end_via_module_subprocess(command: str, expected_code: int) -> None:
    """実 JSON を stdin へ流す ``python -m`` 実行でも同じ判定になり、block のときだけ stdout と stderr を出すこと。"""
    completed = _run_module(json.dumps({"tool_input": {"command": command}}))

    assert completed.returncode == expected_code
    assert bool(completed.stdout) is (expected_code != 0)
    assert bool(completed.stderr) is (expected_code != 0)


def test_oversized_payload_is_fail_closed_end_to_end() -> None:
    """1 MiB を超える実 stdin ペイロードは launcher 経由でも deny する（監査の再現手順そのもの）。"""
    filler = "x" * (1024 * 1024 + 100)
    completed = _run_module(
        json.dumps({"tool_name": "Bash", "tool_input": {"command": "git commit --no-verify"}, "filler": filler})
    )

    assert completed.returncode == 2
    assert "BLOCKED" in completed.stderr


@pytest.mark.parametrize(
    ("stdin", "truncated", "expected_reason"),
    [
        # 1 MiB 超で切り捨てられた入力は、切り捨て後の中身がたまたま parse できても信用せず deny する。
        ("{not-even-json", True, block_no_verify._TRUNCATED_INPUT_MESSAGE),
        # 非空だが JSON として読めない・tool_input 系キーが無い入力も判定不能として deny する。
        ("{not-json", False, block_no_verify._UNPARSEABLE_INPUT_MESSAGE),
        ({"unrelated": "payload"}, False, block_no_verify._MISSING_FIELDS_MESSAGE),
        # 空入力（tty・stdin 未接続）だけは本処理をスキップして通す。
        ("", False, None),
        ({"tool_input": {"command": "git commit --no-verify"}}, False, _BYPASS_REASON),
        # 先勝ちで 1 キーだけ見ると、無害な tool_input と悪意ある toolInput の同居を素通りさせる
        # （docs/adr/shell-analysis-boundary.md の「誤検出を誤通過より選ぶ」。config_protection も全キー走査）。
        ({"tool_input": {"command": "ls"}, "toolInput": {"command": "git commit --no-verify"}}, False, _BYPASS_REASON),
        ({"tool_input": {"command": "ls"}, "toolInput": {"command": "git status"}}, False, None),
        # list 形状のコンテナも走査する（走査しないと素通りする）。
        ({"tool_input": [{"command": f"git commit {NV}"}]}, False, _BYPASS_REASON),
        ({"tool_input": [f"git commit {NV}"]}, False, _BYPASS_REASON),
        ({"tool_input": [{"command": "ls"}, {"command": "git commit -n"}]}, False, _BYPASS_REASON),
        ({"tool_input": [{"command": "ls"}]}, False, None),
    ],
    ids=[
        "truncated",
        "unparseable-json",
        "missing-tool-input",
        "empty-input",
        "bypass",
        "bypass-in-later-container-key",
        "all-container-keys-benign",
        "list-of-dicts",
        "list-of-strings",
        "bypass-in-later-element",
        "benign-list",
    ],
)
def test_main(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stdin: dict | str,
    truncated: bool,
    expected_reason: str | None,
) -> None:
    """deny は host に関わらず exit 2 と理由入りの deny JSON・stderr を同時に出し、通すときは何も出さず exit 0 になること。"""
    raw = stdin if isinstance(stdin, str) else json.dumps(stdin)
    monkeypatch.setattr(block_no_verify, "read_raw_stdin_with_truncation", lambda: (raw, truncated))

    rc = block_no_verify.main()
    captured = capsys.readouterr()

    if expected_reason is None:
        assert (rc, captured.out, captured.err) == (0, "", "")
    else:
        assert rc == 2
        assert json.loads(captured.out) == {"permissionDecision": "deny", "permissionDecisionReason": expected_reason}
        assert captured.err == expected_reason + "\n"


_NESTED = "[" * 200_000 + "]" * 200_000


@pytest.mark.parametrize(
    ("payload", "expected_codes"),
    [
        # 外側の parse_json_object は dict を返し、extract_tool_input 内側の json.loads だけが RecursionError を送出する経路。
        (json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo hi"}, "toolArgs": _NESTED}), {0, 2}),
        # 外側の parse_json_object が RecursionError を受ける経路は、パース不能 = 判定不能なので deny。
        ('{"tool_name":"Bash","tool_input":' + _NESTED + "}", {2}),
        # バイパスフラグを載せた形は tool_input 側の実コマンドが検査され deny される（toolArgs は生文字列に倒れる）。
        (json.dumps({"tool_name": "Bash", "tool_input": {"command": f"git commit {NV} -m x"}, "toolArgs": _NESTED}), {2}),
    ],
    ids=["nested-inside-json-string", "nested-in-outer-payload", "nested-with-bypass-flag"],
)
def test_deep_nesting_does_not_fail_open(monkeypatch: pytest.MonkeyPatch, payload: str, expected_codes: set[int]) -> None:
    """深さ 200,000 の入れ子（1 MiB の内側の 400KB 超）でも exit 1（PreToolUse の non-blocking error = fail-open）へ倒れないこと。"""
    assert len(payload) > 400_000
    monkeypatch.setattr(block_no_verify, "read_raw_stdin_with_truncation", lambda: (payload, False))

    assert block_no_verify.main() in expected_codes


def test_tokenize_falls_back_to_whitespace_split() -> None:
    """クォート不整合時は空白分割へフォールバックする（fail-closed）。"""
    assert block_no_verify.tokenize_with_status('git commit -m "x') == (["git", "commit", "-m", "x"], False)


def test_split_segments_drops_empty_segments() -> None:
    """連続する区切りトークンで空セグメントを作らない。"""
    tokens = ["&&", "git", "status", ";", ";", "git", "commit"]

    assert block_no_verify.split_segments(tokens) == [["git", "status"], ["git", "commit"]]


def test_parse_git_segment_returns_subcommand_and_flags() -> None:
    """グローバルオプションを読み飛ばしてサブコマンドとフラグを返す。"""
    invocation = block_no_verify.parse_git_segment(["git", "-C", "/tmp", "--no-pager", "commit", "-am", "msg", "--no-verify"])

    assert invocation.subcommand == "commit"
    assert invocation.flags == ["-C", "--no-pager", "-a", "-m", "--no-verify"]
    assert invocation.subcommand_certain is True


@pytest.mark.parametrize(
    ("segment", "certain"),
    [
        # 既知の boolean グローバルオプションはサブコマンド解決を乱さない。
        (["git", "--no-pager", "log"], True),
        (["git", "-p", "log"], True),
        # 未知の long / short オプションは値を取るか不明なので解決を信用しない（クラスタ内の 1 文字でも未知なら不確定）。
        (["git", "--future-option", "value", "commit"], False),
        (["git", "-Z", "value", "commit"], False),
        # サブコマンド解決後の未知オプションは解決結果に影響しない。
        (["git", "commit", "--future-option", "value"], True),
        (["git", "push", "-Z"], True),
        # 値が = で結合された未知オプションは次トークンを食わないので確定。
        (["git", "--future-option=value", "commit"], True),
    ],
)
def test_parse_git_segment_subcommand_certainty(segment: list[str], certain: bool) -> None:
    """未知グローバルオプションの有無でサブコマンド確度を切り替える。"""
    assert block_no_verify.parse_git_segment(segment).subcommand_certain is certain


def test_pre_compact_main_appends_to_compaction_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """圧縮イベントを sessions ディレクトリの compaction-log.txt へ日時付きで追記し、記録したことをログへ出すこと。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(pre_compact, "log", logs.append)
    monkeypatch.setattr(pre_compact, "get_datetime_string", lambda: "2026-01-01 00:00:00")

    assert pre_compact.main() == 0
    assert (tmp_path / "compaction-log.txt").read_text(encoding="utf-8") == (
        "[2026-01-01 00:00:00] Context compaction triggered\n"
    )
    assert logs == ["[PreCompact] State saved before compaction"]


def test_pre_compact_main_logs_error_without_blocking(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """追記に失敗してもブロックせず exit 0 を返し、エラーをログへ出すこと。"""
    logs: list[str] = []
    monkeypatch.setattr(pre_compact, "get_sessions_dir", lambda: tmp_path)
    monkeypatch.setattr(pre_compact, "append_file", mock.Mock(side_effect=RuntimeError("boom")))
    monkeypatch.setattr(pre_compact, "log", logs.append)

    assert pre_compact.main() == 0
    assert logs == ["[PreCompact] Error: boom"]


@pytest.mark.parametrize(
    ("module_name", "stdin"),
    [
        ("block_no_verify", json.dumps({"tool_input": {"command": "git status"}})),
        ("config_protection", json.dumps({"tool_input": {"file_path": "README.md"}})),
        ("pre_compact", ""),
    ],
    ids=["block_no_verify", "config_protection", "pre_compact"],
)
def test_entrypoint_exits_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, module_name: str, stdin: str) -> None:
    """各フックを __main__ として起動すると、通る入力で exit 0 を返すこと（pre_compact の記録先は tmp_path）。"""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr("claq.lib.core_utils.get_sessions_dir", lambda: tmp_path)

    assert _run_entrypoint(f"claq.hooks.{module_name}") == 0
