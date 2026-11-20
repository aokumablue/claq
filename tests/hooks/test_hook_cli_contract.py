"""PreToolUse 入力軸のゲートと、実装が読む別名の等号照合。

launcher 経由で hook を起動し、コンテナキー・形状・多重度・フィールド別名・
ツール名別名の各軸を陽性/陰性の対で流す。あわせて `lib/harness.py` が読む
別名を AST で抽出し、ゲート表が流す集合と等号で照合する。起動・往復の
スモークは test_hook_cli_launch_smoke.py の担当。

インストール済み plugin や実 HOME には触れない。COPILOT_TEST=1 で
Copilot の deny JSON を選ばせる。
"""

from __future__ import annotations

import ast
import importlib.util
import json
from pathlib import Path

import pytest

from claq.hooks.bash_config_protection import _BASH_TOOL_NAMES
from claq.hooks.config_protection import _WRITE_TOOL_NAMES
from claq.lib.harness import INPUT_CONTAINER_KEYS, normalize_tool_name

from ._support import _iter_declared_commands, _run_launcher


def _pre_tool_use_modules() -> set[str]:
    """hooks.json の PreToolUse が宣言する dotted module 名を返す。"""
    return {
        next(arg for arg in args if arg.startswith("claq."))
        for event, args, _ in _iter_declared_commands()
        if event == "PreToolUse"
    }


def _assert_discriminates(hook: str, denied_payload: dict, allowed_payload: dict, tmp_path: Path, label: str) -> None:
    """deny されるべき payload が exit 2 と deny JSON、陰性対照が exit 0 になることを launcher 経由で確かめる。

    陰性対照を必須にするのは、両極が同じ exit code を返す測定（両方 0 でも両方 2 でも判別していない）を合格と読ませないため。
    """
    denied = _run_launcher(hook, denied_payload, tmp_path)
    allowed = _run_launcher(hook, allowed_payload, tmp_path)

    assert denied.returncode == 2, f"{label}: deny されるべき payload が exit {denied.returncode} err={denied.stderr[:200]}"
    assert json.loads(denied.stdout)["permissionDecision"] == "deny"
    assert allowed.returncode == 0, f"{label}: 通るべき payload が exit {allowed.returncode} err={allowed.stderr[:200]}"


# --- コンテナキー × 形状 ---------------------------------------------------------
#
# 判定関数の in-process テストは stdin JSON → コンテナキー解決 → 形状展開 → exit code までの経路を通らない。
# プロセス層が dict 形状しか流さないと、`{"tool_input": [{"file_path": "ruff.toml"}]}` や
# `{"tool_input": "ruff.toml"}` の素通りを止められる層が 1 つも無い（`harness_audit` も実効性は測らない）。

# 形状名 → (フィールド名, 値) をコンテナへ包む関数。
_PAYLOAD_SHAPES = {
    "dict": lambda field, value: {field: value},
    "jsonstr": lambda field, value: json.dumps({field: value}),
    "listdict": lambda field, value: [{field: value}],
    "liststr": lambda field, value: [value],
    "barestr": lambda field, value: value,
}

# hook → (tool_name, フィールド名, deny されるべき値, 通るべき値)。
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


@pytest.mark.parametrize("hook", sorted(_SHAPE_GATED_HOOKS))
@pytest.mark.parametrize("container_key", INPUT_CONTAINER_KEYS)
@pytest.mark.parametrize("shape", list(_PAYLOAD_SHAPES))
def test_pre_tool_use_hook_discriminates_across_container_and_shape(
    hook: str, container_key: str, shape: str, tmp_path: Path
) -> None:
    """全コンテナキー × 全形状で、deny されるべき payload が exit 2、陰性対照が exit 0 になること。"""
    tool_name, field, deny_value, allow_value = _SHAPE_GATED_HOOKS[hook]
    wrap = _PAYLOAD_SHAPES[shape]

    _assert_discriminates(
        hook,
        {"tool_name": tool_name, container_key: wrap(field, deny_value)},
        {"tool_name": tool_name, container_key: wrap(field, allow_value)},
        tmp_path,
        f"{hook} {container_key}/{shape}",
    )


# --- コンテナキー多重度 ------------------------------------------------------------
#
# 形状ゲートはキーを 1 つずつしか流さないため、「無害なキーが先頭にあり、危険な入力が後続キーにある」
# payload を作らない。走査層を共有しても消費側が先勝ちなら穴は残るので、全 PreToolUse hook に流す。
#
# hook → (tool_name, フィールド名, 後続キーへ入れる deny 値, 先頭キーへ入れる無害値)。
_MULTIPLICITY_GATED_HOOKS = {
    "claq.hooks.block_no_verify": ("Bash", "command", "git commit --no-verify -m x", "echo safe"),
    "claq.hooks.bash_config_protection": ("Bash", "command", "echo x > ruff.toml", "echo safe"),
    "claq.hooks.config_protection": ("Write", "file_path", "ruff.toml", "sample.py"),
    "claq.hooks.pre_bash_commit_quality": ("Bash", "command", "git add . && git commit -m x", "echo safe"),
}


@pytest.mark.parametrize("hook", sorted(_MULTIPLICITY_GATED_HOOKS))
def test_pre_tool_use_hook_scans_every_container_key(hook: str, tmp_path: Path) -> None:
    """先頭キーが無害でも後続キーに入った危険な入力を deny し、全キー無害の多重コンテナは通すこと。"""
    tool_name, field, deny_value, allow_value = _MULTIPLICITY_GATED_HOOKS[hook]
    first, last = INPUT_CONTAINER_KEYS[0], INPUT_CONTAINER_KEYS[-1]

    _assert_discriminates(
        hook,
        {"tool_name": tool_name, first: {field: allow_value}, last: {field: deny_value}},
        {"tool_name": tool_name, first: {field: allow_value}, last: {field: allow_value}},
        tmp_path,
        f"{hook} 後続コンテナキー",
    )


# --- フィールド別名 ----------------------------------------------------------------
#
# 上の 2 軸はコンテナの中のフィールド名を正規名（command / file_path）に固定している。共有層は別名も受理する
# （`_commands_from_tool_input` の `cmd`、`_extract_patch_text` の生パッチ文字列と `{"input": ...}`）ため、
# 別名側の経路だけ判定が抜けても正規名のテストは緑のまま。別名ごとにコンテナの形が違うので、
# ケースごとに tool_input へ入れるコンテナ値そのものを陽性/陰性の対で持つ。
#
# 登録はフック単位で、別名単位の網羅ではない。`config_protection` の `file`（`file_path` の別名）は
# この表に無く、フィールド多重度ゲートが流している。表の合計が実装の読む別名と一致することは
# `test_field_alias_gate_flows_every_alias_the_implementation_reads` が実装から抽出した集合との等号で強制する。

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


@pytest.mark.parametrize("case_id", sorted(_FIELD_ALIAS_CASES))
def test_pre_tool_use_hook_reads_field_aliases(case_id: str, tmp_path: Path) -> None:
    """別名フィールドに入った危険な入力も deny し、同じ別名フィールドの無害な値は通すこと。"""
    hook, tool_name, deny_input, allow_input = _FIELD_ALIAS_CASES[case_id]

    _assert_discriminates(
        hook,
        {"tool_name": tool_name, "tool_input": deny_input},
        {"tool_name": tool_name, "tool_input": allow_input},
        tmp_path,
        case_id,
    )


# --- ツール名別名 ------------------------------------------------------------------
#
# ここまでの軸は tool 名を Claude Code 表記（Bash / Write）の `tool_name` で送る。実際のハーネスは
# camelCase の `toolName` キーや、`run_terminal_command` / `search_replace` のような runtime 固有名・
# lowercase で送ってくる（`lib/harness.py` の `_TOOL_NAME_MAP`）。別名が正規化から落ちると、
# ツール名でゲートするフックは対象外のツールと見なして早期 return 0 する。
#
# 本体でツール名を判定するのは bash_config_protection と config_protection だけで、block_no_verify と
# pre_bash_commit_quality は hooks.json の matcher に委ねる。後者 2 つの行は **vacuous に緑**（素通り
# しないことは確かめるが、正規化が壊れても赤くならない）で、matcher 依存が崩れて本体判定が入った瞬間に
# 別名を取りこぼさないための前方固定として載せる。前方固定に歯を持たせるため、
# `test_in_hook_tool_name_gate_registration_matches_implementation` が本体でツール名を読むフックの集合を
# 実装から導出して `_IN_HOOK_TOOL_NAME_GATES` との一致を強制し、そこへ載ったフックにだけ
# `test_declared_tool_name_aliases_normalize_into_hook_gate` の正規化検査が及ぶ。
#
# 登録はフック単位で、別名単位の網羅ではない。各フックへ流す別名は `_TOOL_NAME_MAP` の部分集合の写経で、
# `shell` や `multiedit` は含まない（`test_hooks_json_matchers.py` と `tests/lib/test_harness.py` が被覆する）。
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

# hook → (フィールド名, deny されるべき値, 通るべき値, ツール名別名の一覧)。
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

# 本体でツール名を判定する hook → その受理集合。テスト側へ写経すると、実装の受理集合が狭まったときに
# 気付けないため実装から直接参照する。
_IN_HOOK_TOOL_NAME_GATES = {
    "claq.hooks.bash_config_protection": _BASH_TOOL_NAMES,
    "claq.hooks.config_protection": _WRITE_TOOL_NAMES,
}

# 本体でツール名を読んでいることを示す `lib/harness` の symbol。現状の 4 フックはツール名をこの 2 つ経由で
# しか読まない（生の `payload["tool_name"]` 直読みは無い）。
_TOOL_NAME_READER_SYMBOLS = frozenset({"extract_raw_tool_name", "normalize_tool_name"})


def _harness_imports(module: str) -> set[str]:
    """モジュールが `claq.lib.harness` から直 import している symbol 名を AST の ImportFrom から返す。"""
    # 文字列の部分一致にすると docstring 中の言及を実装と誤認するため ImportFrom に限る。検出は symbol の
    # 直 import に限り、`from claq.lib import harness` + `harness.extract_raw_tool_name(...)` の形は
    # すり抜ける（現状 4 フックはいずれも直 import で、module import の前例も無い）。現れたら ast.Attribute
    # も見る必要がある。ツール名読み取りとフィールド別名の導出が同じ限界を共有するよう走査はここ 1 か所に閉じる。
    origin = importlib.util.find_spec(module).origin
    tree = ast.parse(Path(origin).read_text(encoding="utf-8"))
    return {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "claq.lib.harness"
        for alias in node.names
    }


def _hooks_reading_tool_name_in_body() -> set[str]:
    """PreToolUse hook のうち、ツール名読み取り symbol を import して本体でツール名を読むモジュールを返す。"""
    return {
        module
        for module in _pre_tool_use_modules()
        if _harness_imports(module) & _TOOL_NAME_READER_SYMBOLS
    }


def test_in_hook_tool_name_gate_registration_matches_implementation() -> None:
    """本体でツール名を読むフックの集合が `_IN_HOOK_TOOL_NAME_GATES` と一致すること。

    ツール名別名軸の vacuous な 6 行（block_no_verify / pre_bash_commit_quality）を残す判断を支える唯一の機械的な
    根拠。両フックが本体判定を持てばここが赤くなって登録を強制し、受理集合の登録をこっそり消す方向も塞ぐ。
    """
    assert _hooks_reading_tool_name_in_body() == set(_IN_HOOK_TOOL_NAME_GATES), (
        "本体でツール名を読むフックと受理集合の宣言がずれている: "
        f"実装={sorted(_hooks_reading_tool_name_in_body())} "
        f"宣言={sorted(_IN_HOOK_TOOL_NAME_GATES)}"
    )


def test_declared_tool_name_aliases_normalize_into_hook_gate() -> None:
    """宣言した別名が、本体の受理集合へ実際に正規化されること。

    別名の一覧はテスト側の写経なので、`_TOOL_NAME_MAP` から別名が落ちてもプロセス層テストは「対象外ツールなので
    exit 0」を陰性対照として緑に読み、保護が消えたことを緑のまま通す。正規化の対応そのものを突き合わせる。
    """
    for hook, gate in _IN_HOOK_TOOL_NAME_GATES.items():
        _, _, _, aliases = _TOOL_NAME_ALIAS_GATED_HOOKS[hook]
        for name_key, name_value in aliases:
            assert normalize_tool_name(name_value).lower() in gate, (
                f"{hook}: {name_key}={name_value} が正規化後に {sorted(gate)} へ落ちない"
            )


def _tool_name_alias_cases() -> list[tuple[str, str, str]]:
    """(hook, ツール名キー, ツール名値) の全組み合わせを宣言順に列挙する。"""
    return [
        (hook, name_key, name_value)
        for hook, (_, _, _, aliases) in sorted(_TOOL_NAME_ALIAS_GATED_HOOKS.items())
        for name_key, name_value in aliases
    ]


@pytest.mark.parametrize(("hook", "name_key", "name_value"), _tool_name_alias_cases())
def test_pre_tool_use_hook_accepts_tool_name_aliases(hook: str, name_key: str, name_value: str, tmp_path: Path) -> None:
    """ハーネス固有のツール名表記でも危険な入力を deny し、同じ表記の無害な値は通すこと。

    別名が正規化から落ちたときの壊れ方は両極とも exit 0（対象外ツール扱いの早期 return）なので、陰性側だけでは判別できない。
    """
    field, deny_value, allow_value, _ = _TOOL_NAME_ALIAS_GATED_HOOKS[hook]

    _assert_discriminates(
        hook,
        {name_key: name_value, "tool_input": {field: deny_value}},
        {name_key: name_value, "tool_input": {field: allow_value}},
        tmp_path,
        f"{hook} {name_key}={name_value}",
    )


# --- フィールド多重度 --------------------------------------------------------------
#
# フィールド別名ゲートは別名を 1 つずつしか流さないため、「無害な正規フィールドと危険な別名フィールドが
# 同居する」payload を作らない。消費側が先勝ちだと、コンテナキー側の先勝ちバイパスと同型の穴が 1 階層下に
# 残る。dict の挿入順ではなく実装のタプル順が結果を決めるため、危険な側を先に置いても素通りしうる。
#
# hook → (tool_name, 無害フィールド, 無害値, 危険フィールド, 危険値)。
_FIELD_MULTIPLICITY_GATED_HOOKS = {
    "claq.hooks.block_no_verify": ("Bash", "command", "echo safe", "cmd", "git commit --no-verify -m x"),
    "claq.hooks.bash_config_protection": ("Bash", "command", "echo safe", "cmd", "echo x > ruff.toml"),
    "claq.hooks.pre_bash_commit_quality": ("Bash", "command", "echo safe", "cmd", "git add . && git commit -m x"),
    "claq.hooks.config_protection": ("Write", "file_path", "sample.py", "file", "ruff.toml"),
}


@pytest.mark.parametrize("hook", sorted(_FIELD_MULTIPLICITY_GATED_HOOKS))
@pytest.mark.parametrize("dangerous_first", [False, True])
def test_pre_tool_use_hook_scans_every_field_alias(hook: str, dangerous_first: bool, tmp_path: Path) -> None:
    """正規フィールドと別名フィールドが同居しても危険な側を deny し、全フィールド無害なら通すこと。

    dict の挿入順を両方向で流し、順序依存の実装へ戻した場合にどちらの向きでも赤くなるようにする。
    """
    tool_name, safe_field, safe_value, danger_field, danger_value = _FIELD_MULTIPLICITY_GATED_HOOKS[hook]
    fields = [(safe_field, safe_value), (danger_field, danger_value)]

    _assert_discriminates(
        hook,
        {"tool_name": tool_name, "tool_input": dict(fields[::-1] if dangerous_first else fields)},
        {"tool_name": tool_name, "tool_input": {safe_field: safe_value, danger_field: safe_value}},
        tmp_path,
        f"{hook} dangerous_first={dangerous_first}",
    )


@pytest.mark.parametrize(
    ("covered", "exemptions"),
    [
        pytest.param(set(_SHAPE_GATED_HOOKS), _SHAPE_GATE_EXEMPTIONS, id="shape"),
        pytest.param(set(_MULTIPLICITY_GATED_HOOKS), {}, id="container-multiplicity"),
        pytest.param({case[0] for case in _FIELD_ALIAS_CASES.values()}, {}, id="field-alias"),
        pytest.param(set(_TOOL_NAME_ALIAS_GATED_HOOKS), {}, id="tool-name-alias"),
        pytest.param(set(_FIELD_MULTIPLICITY_GATED_HOOKS), {}, id="field-multiplicity"),
    ],
)
def test_every_pre_tool_use_hook_is_gated_on_each_axis(covered: set[str], exemptions: dict[str, str]) -> None:
    """hooks.json の PreToolUse の全 hook が各軸のゲート表に載り、表に hooks.json に無い hook が無いこと。

    新しい保護フックの宣言を忘れると、その hook だけ一部の入力の形しか検証されないまま出荷される。免除は形状軸だけに
    理由つきで置ける。他の軸の消費側の意味論（読むフィールド名・ツール名・先勝ちかどうか）はフックごとの別実装で、
    共有層のゲートでは被覆できないため免除を許さない。
    """
    assert covered | set(exemptions) == _pre_tool_use_modules()
    assert all(exemptions.values()), "免除には理由が要る"


# --- 実装が読む別名と、ゲートが流す別名の等号照合 -----------------------------------
#
# 別名軸は実装が読む別名名をテスト側へ写経している。`INPUT_CONTAINER_KEYS` はモジュール定数なので import で
# 写経を避けられたが、フィールド別名は `_commands_from_tool_input` の `for key in ("command", "cmd")` や
# `extract_file_paths` の `for key in ("file_path", "file")` のような関数本体のインラインリテラルで import
# できない。タプルへ 3 つ目の別名を足しても全ゲートは緑のまま通り、新しい別名だけが 1 度も payload に
# 載らないまま出荷される。`test_declared_tool_name_aliases_normalize_into_hook_gate` は「宣言 ⊆ 実装」しか
# 見ないので、ここは**等号**で照合する。
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

# payload 直下のキーを読む harness 関数。dict キーのリテラルを payload 直下のキーと tool_input のフィールドへ
# 振り分け、両バケットとも別の等号照合を持つ（入れ違えてもいずれかの等号が赤くなるので免除枠は無い）。
_PAYLOAD_KEY_READERS = frozenset({"extract_raw_tool_name"})


def _harness_source() -> str:
    """`claq.lib.harness` のソーステキストを返す。"""
    return Path(importlib.util.find_spec("claq.lib.harness").origin).read_text(encoding="utf-8")


def _dict_key_literals(function: ast.FunctionDef) -> set[str]:
    """関数本体が dict のキーとして読む文字列リテラル（`x.get("key")` と `for key in ("a", "b")`）を集める。"""
    # 片方の形だけを見ると、もう一方へ書き換えた瞬間に検査が無言で外れる。現行 harness が使うのは内包表記の
    # 形だけで `for` 文の側は該当箇所が無いが、書き換えても検査が外れないための前方固定として残す。
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
    """seed から推移的に呼ばれる harness 関数ごとに、読む dict キーのリテラルを返す（読まない関数は含めない）。"""
    # hook が直接 import した関数だけを見ると、その先で読まれる別名を取りこぼす（`config_protection` は
    # `extract_file_paths` しか import しないが、`input` フィールドはその先の `_extract_patch_text` が読む）。
    # source は変異させたソースも渡せる。
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
    """hook が到達する実装が、tool_input のフィールドとして読む別名を返す。"""
    return {
        key
        for name, keys in _key_literals_by_function(source, _harness_imports(hook)).items()
        if name not in _PAYLOAD_KEY_READERS
        for key in keys
    }


def _gate_field_aliases(hook: str) -> set[str]:
    """ゲート表（フィールド別名表と多重度表の実データ）がその hook へ実際に流す tool_input フィールド名を返す。"""
    # 新しい一覧を書き起こすと写経を写経で照合することになり、等号が実装との乖離を捕まえられない。
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

    実装側が増えれば「載せていない別名がある」、ゲート側が増えれば「実装が読まないフィールドを流していて陽性が
    空振りしている」。hook の一覧は hooks.json から取るため、新しい PreToolUse hook は自動でこの照合の対象になる。
    """
    implementation = _implementation_field_aliases(_harness_source(), hook)
    gate = _gate_field_aliases(hook)

    assert implementation == gate, (
        f"{hook}: 実装が読むフィールド別名とゲートが流す別名がずれている: "
        f"実装のみ={sorted(implementation - gate)} ゲートのみ={sorted(gate - implementation)}"
    )


def test_payload_key_reads_match_tool_name_alias_gate() -> None:
    """実装が payload 直下から読むツール名キーと、ツール名別名軸の name_key が一致すること。

    `_PAYLOAD_KEY_READERS` はフィールド別名の等号から外れる唯一のバケットなので、ここで別の等号に載せる。
    ツール名を本体で読むのは 2 フックだけで残る 2 フックは実装側が空集合になるため、hook 単位ではなく
    モジュール単位の等号にする（その非対称の是非は `test_in_hook_tool_name_gate_registration_matches_implementation` の担当）。
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

    常に空集合を返す抽出器でも等号テストはゲート側と噛み合わなくなるまで緑のままなので、緑であることは
    抽出器が動いている証拠にならない。実装を変異させた入力で抽出結果が広がり等号が破れることを実測する。
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
