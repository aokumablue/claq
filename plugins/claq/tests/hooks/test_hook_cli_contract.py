"""DT-06: リポジトリ内 launcher.py 経由の hook 入出力契約。

インストール済み plugin や実 HOME には触れない。COPILOT_TEST=1 で
Copilot の deny JSON を選ばせる。
"""

from __future__ import annotations

import ast
import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from claq.hooks.bash_config_protection import _BASH_TOOL_NAMES
from claq.hooks.config_protection import _WRITE_TOOL_NAMES
from claq.lib.harness import INPUT_CONTAINER_KEYS, normalize_tool_name

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
LAUNCHER = PLUGIN_ROOT / "src" / "claq" / "launcher.py"
HOOK_WRAPPER = PLUGIN_ROOT / "runtime" / "claq-hook"
_ISOLATED_ENV_PREFIXES = ("CODEX_", "GROK_", "COPILOT_")
_ISOLATED_ENV_KEYS = frozenset({
    "CLAUDECODE",
    "PLUGIN_DATA",
    "CLAQ_HOME",
    # mem のデータ位置を決めるのはこちら（src/claq/mem/settings.py）。
    # CLAQ_HOME は lib/core_utils.get_claq_dir() が読む別変数で、
    # これを消しても実 ~/.claq への書き込みは止まらなかった。
    "CLAQ_DATA_PATH",
    "CLAUDE_SESSION_ID",
    "CLAUDE_PROJECT_DIR",
    "CLAUDE_PLUGIN_ROOT",
})


def _launcher_env(tmp_home: Path) -> dict[str, str]:
    """launcher サブプロセス用の隔離環境を組み立てる。

    Args:
        tmp_home: 一時 HOME。実ユーザーの ~/.claq を触らない。

    Returns:
        サブプロセスに渡す環境変数。
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(_ISOLATED_ENV_PREFIXES) or key in _ISOLATED_ENV_KEYS:
            del env[key]
    env["HOME"] = str(tmp_home)
    env["CLAUDE_PLUGIN_ROOT"] = str(PLUGIN_ROOT)
    env["COPILOT_TEST"] = "1"
    return env


def _run_launcher(hook: str, payload: dict, tmp_home: Path) -> subprocess.CompletedProcess[str]:
    """リポジトリ内 launcher で hook モジュールを実行する。

    Args:
        hook: dotted module name（例: claq.hooks.config_protection）。
        payload: stdin に渡す JSON オブジェクト。
        tmp_home: 一時 HOME。

    Returns:
        CompletedProcess。
    """
    return subprocess.run(
        [sys.executable, str(LAUNCHER), hook],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_home),
        timeout=30,
        check=False,
    )


def _assert_denied_ruff(result: subprocess.CompletedProcess[str]) -> None:
    """config_protection が ruff.toml を deny JSON・exit 2 で返したことを検証する。"""
    assert result.returncode == 2
    parsed = json.loads(result.stdout)
    assert parsed["permissionDecision"] == "deny"
    assert "ruff.toml" in parsed["permissionDecisionReason"]


def test_launcher_config_protection_denies_snake_case_payload(tmp_path: Path) -> None:
    """DT-06 #1: snake_case の ruff.toml 編集は deny JSON・exit 2。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"tool_name": "Edit", "tool_input": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_denies_native_camel_case_payload(tmp_path: Path) -> None:
    """DT-06 #2: native camelCase の ruff.toml 編集も同じ deny。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "ruff.toml"}},
        tmp_path,
    )
    _assert_denied_ruff(result)


def test_launcher_config_protection_allows_unprotected_native_file(tmp_path: Path) -> None:
    """DT-06 #3: native camelCase の sample.py は deny なし。"""
    result = _run_launcher(
        "claq.hooks.config_protection",
        {"toolName": "edit", "toolArgs": {"file_path": "sample.py"}},
        tmp_path,
    )
    assert result.returncode == 0
    assert result.stdout == "" or "permissionDecision" not in result.stdout


HOOKS_JSON = PLUGIN_ROOT / "hooks" / "hooks.json"

# 各イベントの最小 valid payload。hooks.json 由来の全エントリを起動して
# 「壊れずに終わる」ことだけを見る（allow/deny の正しさは個別テストの担当）。
_MINIMAL_PAYLOADS = {
    "PreToolUse": {"tool_name": "Bash", "tool_input": {"command": "echo ok"}},
    "PreCompact": {"session_id": "test-session", "transcript_path": "/nonexistent/transcript.jsonl"},
    "SessionStart": {"session_id": "test-session", "source": "startup"},
    "SessionEnd": {"session_id": "test-session", "reason": "clear"},
}


def _iter_declared_commands() -> list[tuple[str, list[str], int]]:
    """hooks.json が宣言する (イベント名, wrapper 引数, timeout) を列挙する。

    Returns:
        宣言順の (event, args, timeout) タプル。args は ``runtime/claq-hook``
        以降の引数。
    """
    declared = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))["hooks"]
    entries: list[tuple[str, list[str], int]] = []
    for event, groups in declared.items():
        for group in groups:
            for hook in group.get("hooks", []):
                tokens = shlex.split(hook["command"])
                wrapper_index = next(i for i, t in enumerate(tokens) if t.endswith(HOOK_WRAPPER.name))
                entries.append((event, tokens[wrapper_index + 1 :], hook.get("timeout", 0)))
    return entries


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_target_is_importable(event: str, args: list[str], timeout: int) -> None:
    """hooks.json が起動する dotted module がすべて import 可能であること。

    モジュール名をテスト側にハードコードすると、hooks.json 側のリネームを
    素通りさせる（テストは緑のまま実 hook が死ぬ）。宣言を単一情報源にする。
    """
    module = next(arg for arg in args if arg.startswith("claq."))
    assert importlib.util.find_spec(module) is not None, f"{event}: {module} が import できない"
    assert timeout > 0, f"{event}: timeout が宣言されていない"


@pytest.mark.parametrize(("event", "args", "timeout"), _iter_declared_commands())
def test_declared_hook_runs_without_traceback(
    event: str, args: list[str], timeout: int, tmp_path: Path
) -> None:
    """hooks.json の全エントリが、最小 payload で例外を出さずに終了すること。

    「起動したら即クラッシュする」は静的 validator では原理的に検出できず、
    本番では保護の静かな無効化として現れる。
    """
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        input=json.dumps(_MINIMAL_PAYLOADS[event]),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert "Traceback" not in result.stderr, f"{event} {args}: {result.stderr[:400]}"
    assert result.returncode in {0, 2}, f"{event} {args}: exit={result.returncode}"


@pytest.mark.parametrize(
    ("hook", "command", "denied"),
    [
        # block_no_verify: git フックのバイパス
        ("claq.hooks.block_no_verify", "git commit --no-verify -m x", True),
        ("claq.hooks.block_no_verify", "git commit -n -m x", True),
        ("claq.hooks.block_no_verify", "git commit -m x", False),
        ("claq.hooks.block_no_verify", "git log -n 5", False),
        # bash_config_protection: リンタ設定の弱体化
        ("claq.hooks.bash_config_protection", "echo x > ruff.toml", True),
        ("claq.hooks.bash_config_protection", "rm .eslintrc", True),
        ("claq.hooks.bash_config_protection", "sudo cp /tmp/x ruff.toml", True),
        ("claq.hooks.bash_config_protection", "cat ruff.toml", False),
        ("claq.hooks.bash_config_protection", "ls -la", False),
        # pre_bash_commit_quality: commit 以外は素通り
        ("claq.hooks.pre_bash_commit_quality", "ls -la", False),
    ],
)
def test_bash_hook_allow_deny_matrix(hook: str, command: str, denied: bool, tmp_path: Path) -> None:
    """Bash 系ブロック hook が、プロセス経路でも期待どおり allow/deny すること。

    in-process の判定関数テストは stdin JSON → exit code → deny JSON 出力までの
    経路を通らない。実際にホストが起動する形で往復させる。
    """
    result = _run_launcher(hook, {"tool_name": "Bash", "tool_input": {"command": command}}, tmp_path)
    if denied:
        assert result.returncode == 2, f"{command}: exit={result.returncode}"
        assert json.loads(result.stdout)["permissionDecision"] == "deny"
    else:
        assert result.returncode == 0, f"{command}: exit={result.returncode} err={result.stderr[:200]}"


def test_session_start_emits_valid_json_and_creates_db(tmp_path: Path) -> None:
    """SessionStart が空 DB から起動して有効な JSON を出し、DB を作ること。

    ここが壊れると全セッションの起動時に知識注入が静かに消える。
    """
    result = subprocess.run(
        [sys.executable, str(LAUNCHER), "claq.mem.cli", "context"],
        input=json.dumps(_MINIMAL_PAYLOADS["SessionStart"]),
        capture_output=True,
        text=True,
        env=_launcher_env(tmp_path),
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr[:400]
    assert json.loads(result.stdout) is not None
    assert (tmp_path / ".claq" / "mem.db").is_file()


# --- PreToolUse の入力形状ゲート -------------------------------------------------
#
# 判定関数の in-process テストは stdin JSON → コンテナキー解決 → 形状展開 →
# exit code までの経路を通らない。v0.9.43 は `{"tool_input": [{"file_path":
# "ruff.toml"}]}` と `{"tool_input": "ruff.toml"}` を exit 0 で素通りさせたまま
# リリースされたが、そのときプロセス層のテストは dict 形状しか流しておらず
# （list 形状 0 件・生文字列形状 0 件）、`harness_audit` も修正前後のツリーを
# 同一 58/58 と採点した（実測）。止められる層が 1 つも無かった。
#
# ここでコンテナキー × 形状の直積を陽性/陰性の対で流し、コミット時に落とす。

_PAYLOAD_SHAPES = ("dict", "jsonstr", "listdict", "liststr", "barestr")


def _wrap_payload(container_key: str, shape: str, field: str, value: str) -> dict:
    """1 つのコンテナキー・形状に値を包んだ hook payload コンテナを組み立てる。

    Args:
        container_key: INPUT_CONTAINER_KEYS のいずれか。
        shape: _PAYLOAD_SHAPES のいずれか。
        field: フックが読むフィールド名（command / file_path）。
        value: そのフィールドへ入れる値。

    Returns:
        コンテナキー 1 つだけを持つ dict。
    """
    if shape == "dict":
        return {container_key: {field: value}}
    if shape == "jsonstr":
        return {container_key: json.dumps({field: value})}
    if shape == "listdict":
        return {container_key: [{field: value}]}
    if shape == "liststr":
        return {container_key: [value]}
    return {container_key: value}


# hook → (tool_name, フィールド名, deny されるべき値, 通るべき値)。
# 陰性対照を必須にするのは、両極が同じ exit code を返す測定を「合格」と
# 読ませないため（両方 0 でも両方 2 でも判別していない）。
_SHAPE_GATED_HOOKS = {
    "claq.hooks.block_no_verify": ("Bash", "command", "git commit --no-verify -m x", "git commit -m x"),
    "claq.hooks.bash_config_protection": ("Bash", "command", "echo x > ruff.toml", "cat sample.py"),
    "claq.hooks.config_protection": ("Write", "file_path", "ruff.toml", "sample.py"),
}

# 形状ゲートに載せない PreToolUse hook と、その理由。
# 空の免除ではなく理由を必須にする（黙って外すと網羅の主張が嘘になる）。
_SHAPE_GATE_EXEMPTIONS = {
    "claq.hooks.pre_bash_commit_quality": (
        "判定が staged 内容に依存し、陽性/陰性の作り分けに git fixture が要る。"
        "**形状軸に限り** iter_bash_commands 共有層で block_no_verify と同一経路のため"
        "同層のゲートで被覆される。多重度軸（複数コンテナキー同時）は消費側の意味論であり"
        "共有層では被覆されないため、下の多重度ゲートには本 hook も含める"
    ),
}


def _pre_tool_use_modules() -> set[str]:
    """hooks.json の PreToolUse が宣言する dotted module 名を返す。"""
    return {
        next(arg for arg in args if arg.startswith("claq."))
        for event, args, _ in _iter_declared_commands()
        if event == "PreToolUse"
    }


def test_every_pre_tool_use_hook_is_shape_gated_or_exempted() -> None:
    """PreToolUse の全 hook が形状ゲートか、理由つき免除のどちらかに属すること。

    新しい保護フックを足したときに宣言を忘れると、その hook だけ dict 形状しか
    検証されないまま出荷される。宣言を強制して、網羅の穴を静かに作らせない。
    """
    declared = _pre_tool_use_modules()
    covered = set(_SHAPE_GATED_HOOKS) | set(_SHAPE_GATE_EXEMPTIONS)
    assert declared <= covered, f"形状ゲート未宣言の PreToolUse hook: {sorted(declared - covered)}"
    assert covered <= declared, f"hooks.json に存在しない hook の宣言: {sorted(covered - declared)}"
    assert all(_SHAPE_GATE_EXEMPTIONS.values()), "免除には理由が要る"


@pytest.mark.parametrize("hook", sorted(_SHAPE_GATED_HOOKS))
@pytest.mark.parametrize("container_key", INPUT_CONTAINER_KEYS)
@pytest.mark.parametrize("shape", _PAYLOAD_SHAPES)
def test_pre_tool_use_hook_discriminates_across_container_and_shape(
    hook: str, container_key: str, shape: str, tmp_path: Path
) -> None:
    """全コンテナキー × 全形状で、deny されるべき payload が exit 2 になること。

    同時に陰性対照が exit 0 であることも確かめる。両極が同じ exit code なら
    その測定は判別していないので、deny 側だけを見て合格にしない。
    """
    tool_name, field, deny_value, allow_value = _SHAPE_GATED_HOOKS[hook]

    denied = _run_launcher(
        hook, {"tool_name": tool_name, **_wrap_payload(container_key, shape, field, deny_value)}, tmp_path
    )
    allowed = _run_launcher(
        hook, {"tool_name": tool_name, **_wrap_payload(container_key, shape, field, allow_value)}, tmp_path
    )

    assert denied.returncode == 2, (
        f"{hook} {container_key}/{shape}: deny されるべき payload が exit {denied.returncode}"
    )
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, (
        f"{hook} {container_key}/{shape}: 通るべき payload が exit {allowed.returncode}"
        f" err={allowed.stderr[:200]}"
    )

# コンテナキー「多重度」の軸。上の形状ゲートはキーを 1 つずつしか流さないため、
# 「無害なキーが先頭にあり、危険な入力が後続キーにある」payload を 1 度も作らない。
# 実測では v0.9.44 の bash_config_protection / pre_bash_commit_quality がこの形で
# exit 0 のまま素通りしており、形状ゲートは緑のままだった。走査層を共有しても
# 消費側が先勝ちなら穴は残るので、多重度は独立した軸として全 PreToolUse hook に流す。
#
# hook → (tool_name, フィールド名, 後続キーへ入れる deny 値, 先頭キーへ入れる無害値)。
_MULTIPLICITY_GATED_HOOKS = {
    "claq.hooks.block_no_verify": ("Bash", "command", "git commit --no-verify -m x", "echo safe"),
    "claq.hooks.bash_config_protection": ("Bash", "command", "echo x > ruff.toml", "echo safe"),
    "claq.hooks.config_protection": ("Write", "file_path", "ruff.toml", "sample.py"),
    "claq.hooks.pre_bash_commit_quality": ("Bash", "command", "git add . && git commit -m x", "echo safe"),
}


def test_every_pre_tool_use_hook_is_multiplicity_gated() -> None:
    """PreToolUse の全 hook が多重度ゲートに載っていること（免除枠を設けない）。

    形状ゲートと違い、こちらは免除を許さない。消費側の意味論はフックごとに
    別実装であり、共有層のゲートでは被覆できないため。
    """
    assert _pre_tool_use_modules() == set(_MULTIPLICITY_GATED_HOOKS)


@pytest.mark.parametrize("hook", sorted(_MULTIPLICITY_GATED_HOOKS))
def test_pre_tool_use_hook_scans_every_container_key(hook: str, tmp_path: Path) -> None:
    """先頭キーが無害でも、後続キーに入った危険な入力を deny すること。

    陰性対照として、全キーが無害な多重コンテナ payload が exit 0 になることも
    確かめる（全部 deny する実装でも緑になる測定を合格にしない）。
    """
    tool_name, field, deny_value, allow_value = _MULTIPLICITY_GATED_HOOKS[hook]
    keys = list(INPUT_CONTAINER_KEYS)

    denied = _run_launcher(
        hook,
        {"tool_name": tool_name, keys[0]: {field: allow_value}, keys[-1]: {field: deny_value}},
        tmp_path,
    )
    allowed = _run_launcher(
        hook,
        {"tool_name": tool_name, keys[0]: {field: allow_value}, keys[-1]: {field: allow_value}},
        tmp_path,
    )

    assert denied.returncode == 2, (
        f"{hook}: 後続コンテナキーの deny 値が exit {denied.returncode} で素通りした"
    )
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, (
        f"{hook}: 全キー無害の payload が exit {allowed.returncode} err={allowed.stderr[:200]}"
    )


# --- PreToolUse のフィールド別名軸 ------------------------------------------------
#
# 上の 2 軸はコンテナ（キー・形状・多重度）だけを動かし、コンテナ**の中**で
# フックが読むフィールド名は正規名（command / file_path）に固定している。
# しかし共有層はどれも別名を受理する: `_commands_from_tool_input` は
# `command` と `cmd`、`_extract_patch_text` は生パッチ文字列と `{"input": ...}`。
# 別名側の経路だけ判定が抜けても正規名のテストは緑のままなので、独立した軸にする。
#
# 別名ごとにコンテナの「形」が違う（dict の別キー / 生文字列 / input dict）ため、
# 上 2 軸のような (フィールド名, 値) の直積では表現できない。ケースごとに
# tool_input へ入れるコンテナ値そのものを陽性/陰性の対で持つ。

_APPLY_PATCH_DENY = '*** Begin Patch\n*** Update File: ruff.toml\n+ignore = ["ALL"]\n*** End Patch\n'
_APPLY_PATCH_ALLOW = "*** Begin Patch\n*** Update File: sample.py\n+x = 1\n*** End Patch\n"

# ケース id → (hook, tool_name, deny されるべきコンテナ, 通るべきコンテナ)。
_FIELD_ALIAS_CASES = {
    "block_no_verify/cmd": (
        "claq.hooks.block_no_verify",
        "Bash",
        {"cmd": "git commit --no-verify -m x"},
        {"cmd": "git commit -m x"},
    ),
    "bash_config_protection/cmd": (
        "claq.hooks.bash_config_protection",
        "Bash",
        {"cmd": "echo x > ruff.toml"},
        {"cmd": "cat sample.py"},
    ),
    "pre_bash_commit_quality/cmd": (
        "claq.hooks.pre_bash_commit_quality",
        "Bash",
        {"cmd": "git add . && git commit -m x"},
        {"cmd": "echo safe"},
    ),
    # apply_patch は Copilot CLI が生パッチ文字列を、他ハーネスが {"input": ...}
    # を送る。どちらも `_extract_patch_text` が吸収する別名なので両方流す。
    "config_protection/apply_patch-bare-string": (
        "claq.hooks.config_protection",
        "apply_patch",
        _APPLY_PATCH_DENY,
        _APPLY_PATCH_ALLOW,
    ),
    "config_protection/apply_patch-input-field": (
        "claq.hooks.config_protection",
        "apply_patch",
        {"input": _APPLY_PATCH_DENY},
        {"input": _APPLY_PATCH_ALLOW},
    ),
}


def test_every_pre_tool_use_hook_is_field_alias_gated() -> None:
    """PreToolUse の**全 hook**がフィールド別名軸に載っていること（免除枠を設けない）。

    形状軸と違い免除を許さない。読むフィールド名はフックごとの選択であり、
    共有層のゲートでは「そのフックが実際に別名を読むか」を被覆できないため。

    強制するのはフック単位の登録であって、別名単位の網羅ではない。
    `config_protection` の `file`（`file_path` の別名）はこの表に無く、下の
    多重度ゲートが流している。「表の合計が実装の読む別名と一致する」ことは
    `test_field_alias_gate_flows_every_alias_the_implementation_reads` が
    実装から抽出した集合との等号で強制する。
    """
    declared = _pre_tool_use_modules()
    covered = {hook for hook, _, _, _ in _FIELD_ALIAS_CASES.values()}
    assert declared <= covered, f"フィールド別名ゲート未宣言の PreToolUse hook: {sorted(declared - covered)}"
    assert covered <= declared, f"hooks.json に存在しない hook の宣言: {sorted(covered - declared)}"


@pytest.mark.parametrize("case_id", sorted(_FIELD_ALIAS_CASES))
def test_pre_tool_use_hook_reads_field_aliases(case_id: str, tmp_path: Path) -> None:
    """正規名ではなく別名フィールドに入った危険な入力も deny されること。

    陰性対照として、同じ別名フィールドに無害な値を入れた payload が exit 0 に
    なることも確かめる（別名を見た瞬間に全部 deny する実装でも緑になる測定を
    合格にしない）。
    """
    hook, tool_name, deny_input, allow_input = _FIELD_ALIAS_CASES[case_id]

    denied = _run_launcher(hook, {"tool_name": tool_name, "tool_input": deny_input}, tmp_path)
    allowed = _run_launcher(hook, {"tool_name": tool_name, "tool_input": allow_input}, tmp_path)

    assert denied.returncode == 2, (
        f"{case_id}: 別名フィールドの deny 値が exit {denied.returncode} で素通りした"
    )
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, (
        f"{case_id}: 通るべき payload が exit {allowed.returncode} err={allowed.stderr[:200]}"
    )


# --- PreToolUse のツール名別名軸 --------------------------------------------------
#
# ここまでの軸は tool 名を常に Claude Code 表記（Bash / Write）の `tool_name` で
# 送っている。実際のハーネスは camelCase の `toolName` キーを使い、値も
# `run_terminal_command` / `search_replace` のような runtime 固有名や lowercase で
# 送ってくる（`lib/harness.py` の `_TOOL_NAME_MAP`）。別名が正規化から落ちると、
# ツール名でゲートするフックは「対象外のツール」と見なして早期 return 0 する。
#
# in-hook でツール名を判定するのは bash_config_protection（`_BASH_TOOL_NAMES`）と
# config_protection（`_WRITE_TOOL_NAMES`）の 2 つだけで、block_no_verify と
# pre_bash_commit_quality は hooks.json の matcher に委ねており本体では tool 名を
# 一切読まない。つまり後者 2 つの本パラメタは **vacuous に緑**である
# ——「素通りしないこと」は確かめているが、正規化が壊れても赤くならない。
# それでも表に載せるのは、matcher 依存という前提が崩れて in-hook 判定が入った
# 瞬間に別名を取りこぼさないための前方固定。
#
# ただし「前方固定」はそれ自体では成立しない。vacuous な行を別のテストが補うと
# 注記しても、補う側の走査対象に当該フックが入る保証が無ければ、注記だけが残って
# 歯が無い状態になる（実際 `test_declared_tool_name_aliases_normalize_into_hook_gate`
# が走査する `_IN_HOOK_TOOL_NAME_GATES` にこの 2 つは含まれない）。そこで
# `test_in_hook_tool_name_gate_registration_matches_implementation` が
# 「本体で tool 名を読むフック」の集合を実装から導出し、`_IN_HOOK_TOOL_NAME_GATES`
# との一致を強制する。matcher 依存をやめた瞬間に登録漏れが赤くなり、そこで
# 初めて正規化の歯（下の normalize テスト）が当該フックへ及ぶ。
#
# hook → (フィールド名, deny されるべき値, 通るべき値, ツール名別名の一覧)。
_BASH_TOOL_NAME_ALIASES = (
    ("toolName", "Bash"),
    ("tool_name", "run_terminal_command"),
    ("tool_name", "bash"),
)
_WRITE_TOOL_NAME_ALIASES = (
    ("toolName", "Write"),
    ("tool_name", "search_replace"),
    ("tool_name", "write"),
)

_TOOL_NAME_ALIAS_GATED_HOOKS = {
    "claq.hooks.block_no_verify": (
        "command", "git commit --no-verify -m x", "git commit -m x", _BASH_TOOL_NAME_ALIASES,
    ),
    "claq.hooks.bash_config_protection": (
        "command", "echo x > ruff.toml", "cat sample.py", _BASH_TOOL_NAME_ALIASES,
    ),
    "claq.hooks.pre_bash_commit_quality": (
        "command", "git add . && git commit -m x", "echo safe", _BASH_TOOL_NAME_ALIASES,
    ),
    "claq.hooks.config_protection": (
        "file_path", "ruff.toml", "sample.py", _WRITE_TOOL_NAME_ALIASES,
    ),
}

# 本体でツール名を判定する hook → その受理集合（実装から直接参照する）。
# 集合をテスト側へ写経すると、実装の受理集合が狭まったときに気付けない。
_IN_HOOK_TOOL_NAME_GATES = {
    "claq.hooks.bash_config_protection": _BASH_TOOL_NAMES,
    "claq.hooks.config_protection": _WRITE_TOOL_NAMES,
}


# 本体でツール名を読んでいることを示す `lib/harness` の symbol。現状の 4 フックは
# ツール名をこの 2 つ経由でしか読まない（生の `payload["tool_name"]` 直読みは無い）。
#
# 検出は symbol 直 import に限る。`from claq.lib import harness` +
# `harness.extract_raw_tool_name(...)` の形は**すり抜ける**（false negative）。
# 現状 4 フックはいずれも直 import で、リポジトリ全体でも module import 形式の
# 前例が無いため未対応にしている。module import が現れたら `ast.Attribute` も
# 見る必要がある——この限界を書かずに「実装から導出」とだけ書くと、下の
# 登録一致テストが本来より広く効いているように読める。
_TOOL_NAME_READER_SYMBOLS = frozenset({"extract_raw_tool_name", "normalize_tool_name"})


def _harness_imports(module: str) -> set[str]:
    """モジュールが `claq.lib.harness` から直 import している symbol 名を返す。

    `lib/harness` からの import を AST で見る。文字列の部分一致にすると
    docstring 中の言及を実装と誤認するため、`ImportFrom` ノードに限定する。

    上のコメントに書いた module import 形式（`from claq.lib import harness`）の
    false negative は本関数に由来する。ツール名読み取りの導出とフィールド別名の
    導出が同じ限界を共有するよう、走査はここ 1 か所に閉じる（2 か所へ書くと
    片方だけ `ast.Attribute` 対応が入って被覆範囲がずれる）。

    Args:
        module: dotted module 名（例: claq.hooks.config_protection）。

    Returns:
        直 import されている harness の symbol 名の集合。
    """
    origin = importlib.util.find_spec(module).origin
    tree = ast.parse(Path(origin).read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "claq.lib.harness"
        for alias in node.names
    }


def _hooks_reading_tool_name_in_body() -> set[str]:
    """PreToolUse hook のうち、本体でツール名を読むモジュールを実装から導出する。

    Returns:
        ツール名読み取り symbol を import している dotted module 名の集合。
    """
    return {
        module
        for module in _pre_tool_use_modules()
        if _harness_imports(module) & _TOOL_NAME_READER_SYMBOLS
    }


def test_every_pre_tool_use_hook_is_tool_name_alias_gated() -> None:
    """PreToolUse の**全 hook**がツール名別名軸に載っていること（免除枠を設けない）。

    強制するのはフック単位の登録であって、別名単位の網羅ではない。各フックへ
    流す別名は `_TOOL_NAME_MAP` の部分集合の写経で、`shell` や `multiedit` は
    含まない（それらは `test_hooks_json_matchers.py` と `tests/lib/test_harness.py`
    が別途被覆する）。写経が実装から乖離していないことは下の normalize テストが見る。
    """
    declared = _pre_tool_use_modules()
    covered = set(_TOOL_NAME_ALIAS_GATED_HOOKS)
    assert declared <= covered, f"ツール名別名ゲート未宣言の PreToolUse hook: {sorted(declared - covered)}"
    assert covered <= declared, f"hooks.json に存在しない hook の宣言: {sorted(covered - declared)}"


def test_in_hook_tool_name_gate_registration_matches_implementation() -> None:
    """本体でツール名を読むフックの集合が `_IN_HOOK_TOOL_NAME_GATES` と一致すること。

    この一致が、ツール名別名軸の 6 行（block_no_verify / pre_bash_commit_quality）を
    vacuous なまま残す判断を支える唯一の機械的な根拠。両フックが matcher 依存を
    やめて本体判定を持った瞬間にここが赤くなり、`_IN_HOOK_TOOL_NAME_GATES` への
    登録——ひいては normalize テストの被覆——を強制する。逆に、受理集合の登録を
    こっそり消して検査範囲を縮める方向も同じ assert が塞ぐ。
    """
    assert _hooks_reading_tool_name_in_body() == set(_IN_HOOK_TOOL_NAME_GATES), (
        "本体でツール名を読むフックと受理集合の宣言がずれている: "
        f"実装={sorted(_hooks_reading_tool_name_in_body())} "
        f"宣言={sorted(_IN_HOOK_TOOL_NAME_GATES)}"
    )


def test_declared_tool_name_aliases_normalize_into_hook_gate() -> None:
    """宣言した別名が、本体の受理集合へ実際に正規化されること。

    別名の一覧はテスト側の写経なので、`_TOOL_NAME_MAP` から別名が落ちても
    プロセス層テストは「対象外ツールなので exit 0」を陰性対照として緑に読み、
    保護が消えたことを緑のまま通してしまう。正規化の対応そのものをここで
    突き合わせて、写経が実装から乖離した時点で赤くする。
    """
    for hook, gate in _IN_HOOK_TOOL_NAME_GATES.items():
        _, _, _, aliases = _TOOL_NAME_ALIAS_GATED_HOOKS[hook]
        for name_key, name_value in aliases:
            assert normalize_tool_name(name_value).lower() in gate, (
                f"{hook}: {name_key}={name_value} が正規化後に {sorted(gate)} へ落ちない"
            )


def _tool_name_alias_cases() -> list[tuple[str, str, str]]:
    """(hook, ツール名キー, ツール名値) の全組み合わせを宣言順に列挙する。

    Returns:
        parametrize へ渡すタプルのリスト。
    """
    return [
        (hook, name_key, name_value)
        for hook, (_, _, _, aliases) in sorted(_TOOL_NAME_ALIAS_GATED_HOOKS.items())
        for name_key, name_value in aliases
    ]


@pytest.mark.parametrize(("hook", "name_key", "name_value"), _tool_name_alias_cases())
def test_pre_tool_use_hook_accepts_tool_name_aliases(
    hook: str, name_key: str, name_value: str, tmp_path: Path
) -> None:
    """ハーネス固有のツール名表記で送られても、危険な入力が deny されること。

    陰性対照として同じツール名表記の無害な値が exit 0 になることも確かめる。
    ここが両極とも 0 になるのが、別名が正規化から落ちたときの壊れ方
    （対象外ツール扱いで早期 return）なので、陰性側だけでは判別できない。
    """
    field, deny_value, allow_value, _ = _TOOL_NAME_ALIAS_GATED_HOOKS[hook]

    denied = _run_launcher(hook, {name_key: name_value, "tool_input": {field: deny_value}}, tmp_path)
    allowed = _run_launcher(hook, {name_key: name_value, "tool_input": {field: allow_value}}, tmp_path)

    assert denied.returncode == 2, (
        f"{hook} {name_key}={name_value}: deny されるべき payload が exit {denied.returncode}"
    )
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, (
        f"{hook} {name_key}={name_value}: 通るべき payload が exit {allowed.returncode}"
        f" err={allowed.stderr[:200]}"
    )


# フィールド「多重度」の軸。上のフィールド別名ゲートは別名を 1 つずつしか流さない
# ため、「無害な正規フィールドと危険な別名フィールドが同居する」payload を作らない。
# 実測では 4 フックすべてがこの形で exit 0 のまま素通りしていた（コンテナキー側で
# 塞いだ先勝ちバイパスと同型のものが 1 階層下に残っていた）。dict の挿入順ではなく
# 実装のタプル順が結果を決めるため、危険な側を先に置いても素通りしていた。
#
# hook → (tool_name, 無害フィールド, 無害値, 危険フィールド, 危険値)。
_FIELD_MULTIPLICITY_GATED_HOOKS = {
    "claq.hooks.block_no_verify": ("Bash", "command", "echo safe", "cmd", "git commit --no-verify -m x"),
    "claq.hooks.bash_config_protection": ("Bash", "command", "echo safe", "cmd", "echo x > ruff.toml"),
    "claq.hooks.pre_bash_commit_quality": ("Bash", "command", "echo safe", "cmd", "git add . && git commit -m x"),
    "claq.hooks.config_protection": ("Write", "file_path", "sample.py", "file", "ruff.toml"),
}


def test_every_pre_tool_use_hook_is_field_multiplicity_gated() -> None:
    """PreToolUse の全 hook がフィールド多重度ゲートに載っていること（免除枠なし）。"""
    assert _pre_tool_use_modules() == set(_FIELD_MULTIPLICITY_GATED_HOOKS)


@pytest.mark.parametrize("hook", sorted(_FIELD_MULTIPLICITY_GATED_HOOKS))
@pytest.mark.parametrize("dangerous_first", [False, True])
def test_pre_tool_use_hook_scans_every_field_alias(hook: str, dangerous_first: bool, tmp_path: Path) -> None:
    """正規フィールドと別名フィールドが同居しても危険な側を deny すること。

    dict の挿入順を両方向で流す。実装はタプル順で走査するため挿入順に依存しないが、
    順序依存の実装へ戻した場合にどちらの向きでも赤くなるようにしておく。
    """
    tool_name, safe_field, safe_value, danger_field, danger_value = _FIELD_MULTIPLICITY_GATED_HOOKS[hook]
    fields = (
        {danger_field: danger_value, safe_field: safe_value}
        if dangerous_first
        else {safe_field: safe_value, danger_field: danger_value}
    )

    denied = _run_launcher(hook, {"tool_name": tool_name, "tool_input": fields}, tmp_path)
    allowed = _run_launcher(
        hook,
        {"tool_name": tool_name, "tool_input": {safe_field: safe_value, danger_field: safe_value}},
        tmp_path,
    )

    assert denied.returncode == 2, (
        f"{hook}: 別名フィールドの deny 値が exit {denied.returncode} で素通りした"
        f" (dangerous_first={dangerous_first}) err={denied.stderr[:200]}"
    )
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, (
        f"{hook}: 全フィールド無害の payload が exit {allowed.returncode} err={allowed.stderr[:200]}"
    )


# --- 実装が読む別名と、ゲートが流す別名の等号照合 -----------------------------------
#
# ここまでの別名軸（フィールド別名 / 多重度）は、実装が読む別名名を**テスト側へ
# 写経**している。`INPUT_CONTAINER_KEYS` はモジュール定数なので import で写経を
# 避けられたが、フィールド別名は `_commands_from_tool_input` の
# `for key in ("command", "cmd")` と `extract_file_paths` の
# `for key in ("file_path", "file")` のように**関数本体のインラインリテラル**で、
# import できない。そのためタプルへ 3 つ目の別名を足しても、上の全ゲートは緑のまま
# 通る——新しい別名だけが 1 度も payload に載らない状態で出荷される。
#
# ツール名別名軸の `test_declared_tool_name_aliases_normalize_into_hook_gate` は
# 「宣言した別名が実装の受理集合へ落ちる」方向（宣言 ⊆ 実装）しか見ておらず、
# 実装が増えた側（実装 ⊄ 宣言）を捕まえない。ここは**等号**で照合する。
#
# 抽出の限界（この節が本来より広く効いていると読ませないため明記する）:
# - seed は `_harness_imports` に依存するため module import 形式はすり抜ける
# - 呼び出しグラフの**起点として辿る**のは harness のモジュール直下 `def` のみ
#   （到達した関数の内側にあるネスト関数のリテラルは `ast.walk` で拾える）
# - 呼び出しの追跡は `ast.Name` 形式の直接呼び出しのみ（動的ディスパッチは追わない）
# - **置換的**な書き換え（タプルを別の形へ移す）は必ず赤くなるが、認識形を残したまま
#   **加算的**に読み足す形——`("command", "cmd", *_EXTRA)` の星付き展開や
#   `if "commandLine" in tool_input:` + 添字——は等号が成立したまま素通りする
# - 照合の宇宙は `lib/harness.py` に閉じる。フック本体が直接読むフィールドは
#   どちらの等号にも載らない（現状 path/command 別名を本体で読むフックは 0 件。
#   `config_protection` の `content` / `edits` 等は別名軸ではなく内容走査軸）

# dict キーとして読まれる文字列リテラルを、payload 直下のキーと tool_input の
# フィールドに振り分ける。両バケットとも別の等号照合を持つため、免除枠は無い
# ——どちらのバケットへ入れ違えても、いずれかの等号が赤くなる。
_PAYLOAD_KEY_READERS = frozenset({"extract_raw_tool_name"})


def _harness_source() -> str:
    """`claq.lib.harness` のソーステキストを返す。

    Returns:
        harness モジュールのソース全文。
    """
    return Path(importlib.util.find_spec("claq.lib.harness").origin).read_text(encoding="utf-8")


def _dict_key_literals(function: ast.FunctionDef) -> set[str]:
    """関数本体が dict のキーとして読む文字列リテラルを集める。

    2 つの形を拾う。``x.get("key")`` の定数引数と、``for key in ("a", "b")``
    の文字列リテラルタプル（内包表記・`for` 文の双方）。後者は実装が別名を
    走査する現行の書き方で、前者は単一キーを直接読む書き方。片方だけを見ると、
    もう一方へ書き換えた瞬間に検査が無言で外れる。

    現行 harness が使うのは内包表記の形だけで、`for` 文の側は該当箇所が無い
    （テストファイルは coverage 計測対象外のため、この非対称は 100% ゲートに
    現れない）。内包表記から `for` 文へ書き換えても検査が外れないための前方固定
    として残す。

    Args:
        function: harness のモジュール直下 `def` ノード。

    Returns:
        キーとして読まれる文字列リテラルの集合。
    """
    keys: set[str] = set()
    for node in ast.walk(function):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            keys.add(node.args[0].value)
        if isinstance(node, ast.For | ast.comprehension) and isinstance(node.iter, ast.Tuple):
            keys.update(
                element.value
                for element in node.iter.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            )
    return keys


def _key_literals_by_function(source: str, seeds: set[str]) -> dict[str, set[str]]:
    """seed から到達する harness 関数ごとに、読む dict キーのリテラルを集める。

    seed（hook が直 import した symbol）から harness 内の呼び出しを推移的に辿る。
    hook が直接 import した関数だけを見ると、その先で読まれる別名を取りこぼす
    （`config_protection` は `extract_file_paths` しか import しないが、実際の
    `input` フィールドはその先の `_extract_patch_text` が読む）。

    Args:
        source: harness のソーステキスト。変異させたソースも渡せる。
        seeds: 起点にする harness の symbol 名。

    Returns:
        関数名 → その関数が読む dict キーの集合（読まない関数は含めない）。
    """
    functions = {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}
    reached: set[str] = set()
    stack = [name for name in seeds if name in functions]
    while stack:
        name = stack.pop()
        if name in reached:
            continue
        reached.add(name)
        stack.extend(
            node.func.id
            for node in ast.walk(functions[name])
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions
        )
    return {name: keys for name in reached if (keys := _dict_key_literals(functions[name]))}


def _implementation_field_aliases(source: str, hook: str) -> set[str]:
    """hook が到達する実装が、tool_input のフィールドとして読む別名を返す。

    Args:
        source: harness のソーステキスト。
        hook: dotted module 名。

    Returns:
        tool_input フィールド別名の集合。
    """
    return {
        key
        for name, keys in _key_literals_by_function(source, _harness_imports(hook)).items()
        if name not in _PAYLOAD_KEY_READERS
        for key in keys
    }


def _gate_field_aliases(hook: str) -> set[str]:
    """ゲート表がその hook へ実際に流す tool_input フィールド名を返す。

    別名表と多重度表の**実データ**から集める。ここで新しい一覧を書き起こすと
    写経を写経で照合することになり、等号が実装との乖離を捕まえられない。

    Args:
        hook: dotted module 名。

    Returns:
        payload に載るフィールド名の集合。
    """
    aliases: set[str] = set()
    for case_hook, _, deny_input, allow_input in _FIELD_ALIAS_CASES.values():
        if case_hook != hook:
            continue
        for container in (deny_input, allow_input):
            # 生パッチ文字列のケースは dict ではなくフィールド名を持たない。
            # コンテナ「形状」軸の担当であり、フィールド別名は寄与しない。
            if isinstance(container, dict):
                aliases.update(container)
    # 未登録の hook を素の KeyError で落とすと、「別名がずれている」のか
    # 「表に載っていない」のかが例外型からしか読めない。登録漏れとして名指しする。
    assert hook in _FIELD_MULTIPLICITY_GATED_HOOKS, f"多重度ゲート未登録の hook: {hook}"
    _, safe_field, _, danger_field, _ = _FIELD_MULTIPLICITY_GATED_HOOKS[hook]
    aliases.update({safe_field, danger_field})
    return aliases


@pytest.mark.parametrize("hook", sorted(_pre_tool_use_modules()))
def test_field_alias_gate_flows_every_alias_the_implementation_reads(hook: str) -> None:
    """実装が読むフィールド別名と、ゲートが流す別名が一致すること。

    実装側が増えれば「載せていない別名がある」、ゲート側が増えれば「実装が
    読まないフィールドを流していて陽性が空振りしている」。どちらの向きの乖離も
    等号で赤くする。hook の一覧は hooks.json から取るため、新しい PreToolUse
    hook を足すと自動でこの照合の対象になる。
    """
    implementation = _implementation_field_aliases(_harness_source(), hook)
    gate = _gate_field_aliases(hook)

    assert implementation == gate, (
        f"{hook}: 実装が読むフィールド別名とゲートが流す別名がずれている: "
        f"実装のみ={sorted(implementation - gate)} ゲートのみ={sorted(gate - implementation)}"
    )


def test_payload_key_reads_match_tool_name_alias_gate() -> None:
    """実装が payload 直下から読むツール名キーと、ツール名別名軸の name_key が一致すること。

    `_PAYLOAD_KEY_READERS` はフィールド別名の等号から外れる唯一のバケットなので、
    外した先を宙に浮かせず、ここで別の等号に載せる。`extract_raw_tool_name` が
    3 つ目のキーを読み始めたら、そのキーを流すゲート行が無い限り赤くなる。

    hook 単位ではなくモジュール単位の等号にする。ツール名を本体で読むのは
    `_IN_HOOK_TOOL_NAME_GATES` の 2 フックだけで、残る 2 フックは matcher 依存の
    ため実装側が空集合になる（その非対称の是非は
    `test_in_hook_tool_name_gate_registration_matches_implementation` の担当）。
    """
    implementation = {
        key
        for hook in _pre_tool_use_modules()
        for name, keys in _key_literals_by_function(_harness_source(), _harness_imports(hook)).items()
        if name in _PAYLOAD_KEY_READERS
        for key in keys
    }
    gate = {
        name_key
        for _, _, _, aliases in _TOOL_NAME_ALIAS_GATED_HOOKS.values()
        for name_key, _ in aliases
    }

    assert implementation == gate, (
        f"実装が読む payload 直下キーとツール名別名軸の name_key がずれている: "
        f"実装のみ={sorted(implementation - gate)} ゲートのみ={sorted(gate - implementation)}"
    )


# 変異テスト用。実装の別名タプルへ 3 つ目を足した状態を作る。
_COMMAND_ALIAS_TUPLE = 'for key in ("command", "cmd")'
_WIDENED_COMMAND_ALIAS_TUPLE = 'for key in ("command", "cmd", "commandLine")'


def test_field_alias_gate_reddens_when_implementation_gains_an_alias() -> None:
    """実装へ別名を 1 つ足すと、上の等号が実際に破れること。

    等号テストが緑であることは、それ自体では「抽出器が動いている」証拠に
    ならない——常に空集合を返す抽出器でも、ゲート側と噛み合わなくなるまでは
    気付けない。実装を変異させた入力を通して、抽出結果が広がり等号が破れる
    ことをここで実測する。上の等号の歯がどこから来ているかの唯一の機械的根拠。
    """
    original = _harness_source()
    mutated = original.replace(_COMMAND_ALIAS_TUPLE, _WIDENED_COMMAND_ALIAS_TUPLE, 1)
    assert mutated != original, (
        f"変異が当たっていない（実装のタプル表記が変わった）: {_COMMAND_ALIAS_TUPLE!r} が見つからない"
    )

    widened = {
        hook
        for hook in _pre_tool_use_modules()
        if _implementation_field_aliases(mutated, hook) != _implementation_field_aliases(original, hook)
    }
    assert widened, "変異した別名がどの hook の抽出結果にも現れない（抽出器が実装を読めていない）"

    for hook in sorted(widened):
        assert _implementation_field_aliases(mutated, hook) != _gate_field_aliases(hook), (
            f"{hook}: 実装が別名を 1 つ増やしてもゲートとの等号が破れない"
        )
