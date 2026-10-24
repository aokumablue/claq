"""保護フックの語彙集合の固定と、個別の迂回回帰。

保護フックの語彙集合（`config_protection` / `bash_config_protection` / `block_no_verify` /
`commit_risk` / `shell_tokens` / `commit_quality_scanner` ほか）を層2（完全一致）と層1（全要素を
フックへ流して判定を実測）で固定し、`claq.hooks` 配下の語彙集合の台帳が漏れなく宣言されていることを
検査する。期待値表（`_EXPECTED_VOCABULARIES` と `_EXTERNALLY_PINNED_VOCABULARIES`）は
`_support.py` に置き、保護ファイル名の全数検査（`test_protection_bypass_regressions.py`）と共有する。
末尾は env 注入・実行予算超過の個別の迂回回帰。
"""

from __future__ import annotations

import ast
import importlib
import pkgutil
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import claq.hooks
from claq.hooks import bash_config_protection, commit_quality_scanner, commit_risk, shell_tokens

from ._support import (
    _EXPECTED_VOCABULARIES,
    _EXTERNALLY_PINNED_VOCABULARIES,
    _NO_VERIFY,
    _bash,
    _run,
    _write,
    _write_content,
)
from ._support import _repo_scope as _repo_scope

# --------------------------------------------------------------------------
# 全語彙集合の固定
#
# `test_protection_bypass_regressions.py` の全数検査は保護**ファイル名**の集合だけを固定する。
# 同じ盲点は保護フックの他の語彙集合すべてに残っている: 集合リテラルは複数行にまたがっても 1 statement なので、
# 要素を 1 個消しても実行行は変わらず `fail_under=100` では原理的に検出できない。
# 代表値をフックへ流す検査（`_BLOCKED_CASES`）はサンプルしか見ないため、消えた
# 要素がサンプル外なら緑のまま通る。
#
# 検査は保護ファイル名の全数検査と同じ 2 層で行う。
#
#   層2（固定）: チェックイン済みの期待値に対する**完全一致**。語彙の増減を
#       人間の意識的な変更に限定する関門。これが本項目の非交渉部分。
#   層1（列挙）: 期待値の**全要素**をフックへ流し、要素ごとに判定が変わることを
#       実測する。「集合には載っているが実際には効いていない」要素を検出する。
#
# 層1 の入力は必ず期待値（`_EXPECTED_VOCABULARIES`）側から取る。実装の集合を
# parametrize の入力にすると、要素を消したときにテストケースも一緒に消えて
# スイートが緑のまま通る。
#
# さらに「期待値表そのものから行を消す」という 1 段上の盲点を
# `test_vocabulary_inventory_is_complete` が塞ぐ。`claq.hooks` 配下の全モジュールを
# 走査して、モジュール自身が定義した文字列コレクション定数を機械的に数え上げ、
# 期待値表の見出し集合と完全一致することを要求する。新しい語彙集合を実装へ足して
# 表へ足し忘れた場合も、表から行を消した場合も、ここで落ちる。
# --------------------------------------------------------------------------

def _is_vocabulary(value: Any) -> bool:
    """値が「文字列の語彙集合」かを判定する。

    要素がすべて文字列の集合／タプル（``PROTECTED_FILES`` 等）と、要素が
    すべて文字列タプルの集合／タプル（``_PROTECTED_PATH_SEGMENTS`` 等）を
    語彙として扱う。空の集合は語彙とみなさない（判定材料が無いため）。

    Args:
        value: モジュール属性の値。

    Returns:
        語彙集合なら True。
    """
    if not isinstance(value, (frozenset, set, tuple)) or not value:
        return False
    if all(isinstance(element, str) for element in value):
        return True
    return all(
        isinstance(element, tuple) and all(isinstance(part, str) for part in element)
        for element in value
    )


def _declared_vocabularies(module: ModuleType) -> dict[str, Any]:
    """モジュール**自身が定義した**語彙集合を返す。

    `vars(module)` だけを見ると import した名前（`bash_config_protection` へ
    import された `PROTECTED_FILES_FOLDED` 等）まで数え上げ、同じ集合が定義元と
    参照側の両方で期待値を要求されてしまう。ソースを AST で読み、モジュール
    トップレベルの代入文で束縛された名前だけに絞る。

    Args:
        module: 対象モジュール。

    Returns:
        ``{定数名: 値}``。語彙集合でない属性は含まない。
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    declared: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            declared |= {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            declared.add(node.target.id)
    return {
        name: getattr(module, name)
        for name in sorted(declared)
        if _is_vocabulary(getattr(module, name, None))
    }


def _hook_modules() -> dict[str, ModuleType]:
    """`claq.hooks` 配下の全モジュールを import して返す。

    モジュール名を表へ書き写さず実際のパッケージから数え上げるため、新しい
    フックモジュールを足しても語彙の数え上げから漏れない。

    Returns:
        ``{モジュール名: モジュール}``。
    """
    return {
        info.name: importlib.import_module(f"claq.hooks.{info.name}")
        for info in pkgutil.iter_modules(claq.hooks.__path__)
    }


def test_vocabulary_inventory_is_complete() -> None:
    """`claq.hooks` 配下の語彙集合が 1 つ残らず期待値表に載っていること（層2 の関門）。

    層2 の完全一致検査は「表に載っている集合」しか守れない。表から行を消せば、
    その集合は無検査に戻る（層1 にある罠と同じものが 1 段上で
    再現する）。実装側を機械的に数え上げて見出し集合と突き合わせることで、
    表の行を消す・新しい語彙を足して表へ書き忘れる、のどちらも赤くする。
    """
    actual = {
        f"{module_name}.{name}"
        for module_name, module in _hook_modules().items()
        for name in _declared_vocabularies(module)
    }
    assert actual == set(_EXPECTED_VOCABULARIES) | set(_EXTERNALLY_PINNED_VOCABULARIES)


@pytest.mark.parametrize("qualified", sorted(_EXTERNALLY_PINNED_VOCABULARIES))
def test_externally_pinned_vocabulary_still_exists(qualified: str) -> None:
    """完全一致検査を免除した語彙が、実装側に今も存在すること。

    免除の一覧は「表に載せない理由」を書く場所であって、消えた集合を隠す場所では
    ない。名前ごと消えたら免除の前提（外部の突き合わせ先がある）も消えている。

    Args:
        qualified: ``<モジュール名>.<定数名>``。
    """
    module_name, _, name = qualified.partition(".")
    assert _is_vocabulary(getattr(_hook_modules()[module_name], name))


@pytest.mark.parametrize("qualified", sorted(_EXPECTED_VOCABULARIES))
def test_vocabulary_is_exactly_as_declared(qualified: str) -> None:
    """語彙集合の内容が完全一致で固定されていること（層2）。

    集合リテラルは複数行でも 1 statement なので、要素を消しても実行行は変わらず
    `fail_under=100` では検出できない。語彙の増減をここでレビュー対象へ上げる。

    Args:
        qualified: ``<モジュール名>.<定数名>``。
    """
    module_name, _, name = qualified.partition(".")
    assert getattr(_hook_modules()[module_name], name) == _EXPECTED_VOCABULARIES[qualified]


def _fill(template: str, item: str) -> str:
    """テンプレート中の ``{item}`` を要素で置換する。

    `str.format` は使わない。payload に JSON（``{"eslintConfig": {}}``）を載せる
    ケースがあり、波括弧のエスケープでテンプレートが読めなくなるため。

    Args:
        template: ``{item}`` を含む文字列。
        item: 埋め込む語彙要素。

    Returns:
        置換後の文字列。
    """
    return template.replace("{item}", item)


# 層1（deny / allow の対）: (語彙の完全修飾名, フック名, deny 書式, allow 書式)。
#
# allow 側は必ず「同じ語を使い、判定軸だけを外した」形にする。そうしないと
# 「全部 deny する」実装でも deny 側だけが緑になり、要素が効いている証拠にならない。
_VOCABULARY_DENY_ALLOW_CASES = (
    # --- 保護 config への書込み verb（bash_config_protection） ---
    ("bash_config_protection._REMOVE_COMMANDS", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("bash_config_protection._MODE_COMMANDS", "bash_config_protection", "{item} 000 ruff.toml", "{item} 000 app.py"),
    ("bash_config_protection._TEE_COMMANDS", "bash_config_protection", "echo x | {item} ruff.toml", "echo x | {item} app.py"),
    (
        "bash_config_protection._LAST_ARG_WRITE_COMMANDS",
        "bash_config_protection",
        "{item} src.txt ruff.toml",
        "{item} src.txt app.py",
    ),
    ("bash_config_protection._TOUCH_COMMANDS", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("bash_config_protection._LN_COMMANDS", "bash_config_protection", "{item} -s weak.toml ruff.toml", "{item} -s weak.toml app.py"),
    ("bash_config_protection._DD_COMMANDS", "bash_config_protection", "{item} if=/dev/null of=ruff.toml", "{item} if=/dev/null of=app.py"),
    # in-place 側は「フラグを外すと allow」を対にする（フラグ判定が生きている証拠）。
    (
        "bash_config_protection._INPLACE_EDIT_COMMANDS",
        "bash_config_protection",
        "{item} -i s/x/y/ ruff.toml",
        "{item} s/x/y/ ruff.toml",
    ),
    # wrapper は「実行位置に無ければ allow」を対にする（実行位置の判定軸）。
    ("bash_config_protection._COMMAND_POSITION_WRAPPERS", "bash_config_protection", "{item} rm ruff.toml", "echo {item} rm ruff.toml"),
    (
        "bash_config_protection._WRAPPER_VALUE_SHORT_OPTIONS",
        "bash_config_protection",
        "sudo {item} root rm ruff.toml",
        "echo sudo {item} root rm ruff.toml",
    ),
    # --- hook_common / shell_tokens の共有語彙 ---
    ("hook_common.ALWAYS_MUTATING_EDIT_EXECUTABLES", "bash_config_protection", "{item} ruff.toml", "{item} app.py"),
    ("shell_tokens.SHELL_WRAPPER_EXECUTABLES", "block_no_verify", "{item} -c '" + _NO_VERIFY + "'", "{item} -c 'git commit -m x'"),
    (
        "shell_tokens._BODY_EXECUTING_BUILTINS",
        "block_no_verify",
        "{item} /dev/stdin <<'EOF'\n" + _NO_VERIFY + "\nEOF",
        "cat /dev/stdin <<'EOF'\n" + _NO_VERIFY + "\nEOF",
    ),
    # 区切りが効かないとセグメントが融合し、後続コマンドが実行位置に来ない。
    ("shell_tokens._SHELL_SEPARATORS", "bash_config_protection", "cat notes.txt {item} rm ruff.toml", "cat notes.txt {item} rm app.py"),
    # --- block_no_verify の config 注入経路 ---
    ("block_no_verify._SENSITIVE_CONFIG_KEY_PREFIXES", "block_no_verify", "git -c {item}=x commit -m y", "git -c user.name=x commit -m y"),
    ("block_no_verify._GIT_CONFIG_INJECTION_ENV_NAMES", "block_no_verify", "{item}=x git commit -m y", "SOME_VAR=x git commit -m y"),
    ("block_no_verify._GIT_CONFIG_WRITE_FLAGS", "block_no_verify", "git config {item} core.hooksPath x", "git config {item} user.name x"),
    ("block_no_verify._GIT_CONFIG_NEW_WRITE_OPS", "block_no_verify", "git config {item} core.hooksPath x", "git config {item} user.name x"),
    # --- commit 前 mutation ガード ---
    (
        "commit_risk._INDEX_MUTATING_GIT_SUBCOMMANDS",
        "pre_bash_commit_quality",
        "git {item} && git commit -m 'fix: x'",
        "git status && git commit -m 'fix: x'",
    ),
    (
        "commit_risk._WORKTREE_MUTATING_EXECUTABLES",
        "pre_bash_commit_quality",
        "{item} app.py && git commit -m 'fix: x'",
        "cat app.py && git commit -m 'fix: x'",
    ),
    (
        "commit_risk._INPLACE_EDIT_EXECUTABLES",
        "pre_bash_commit_quality",
        "{item} -i s/a/b/ app.py && git commit -m 'fix: x'",
        "{item} s/a/b/ app.py && git commit -m 'fix: x'",
    ),
)

# 層1（allow のみ + 陽性対照）: (語彙の完全修飾名, フック名, allow 書式, 対照 deny コマンド)。
#
# 「値を取るオプション」「値を取らないことが確定しているオプション」の語彙は、
# 要素を消すと**未知オプション**として fail-closed（deny）へ倒れる。したがって
# 判定軸が生きている証拠は allow 側にしか出ない。allow が空虚でないことは、
# 同じフックが対照コマンドを deny することで示す。
_VOCABULARY_ALLOW_ONLY_CASES = (
    ("block_no_verify._VALUE_LONG_OPTIONS", "block_no_verify", "git commit {item} --no-verify", _NO_VERIFY),
    ("block_no_verify._BOOLEAN_GLOBAL_LONG_OPTIONS", "block_no_verify", "git {item} log -n 5", "git --future-global-option v commit -n"),
    ("block_no_verify._VALUE_SHORT_OPTIONS", "block_no_verify", "git commit -{item} --no-verify", _NO_VERIFY),
    ("block_no_verify._BOOLEAN_GLOBAL_SHORT_OPTIONS", "block_no_verify", "git -{item} log -n 5", "git -Z log -n 5"),
    ("block_no_verify._OPTIONAL_VALUE_SHORT_OPTIONS", "block_no_verify", "git -{item} log -n 5", "git -Z log -n 5"),
    ("block_no_verify._GIT_CONFIG_READ_ONLY_FLAGS", "block_no_verify", "git config {item} core.hooksPath", "git config --add core.hooksPath x"),
    ("block_no_verify._GIT_CONFIG_NEW_READ_OPS", "block_no_verify", "git config {item} core.hooksPath", "git config set core.hooksPath x"),
    # 行コメントの開始条件。直前文字が語彙に無ければ `#` は語の一部なので deny。
    ("shell_tokens._COMMENT_PRECEDING_CHARS", "block_no_verify", "echo ok{item}# " + _NO_VERIFY, "echo okX# " + _NO_VERIFY),
)

# 層1（Write 系 payload）: (語彙の完全修飾名, deny payload の組み立て, allow payload)。
_VOCABULARY_WRITE_CASES = (
    (
        "config_protection._WRITE_TOOL_NAMES",
        lambda item: {"tool_name": item, "tool_input": {"file_path": "ruff.toml", "content": "x"}},
        {"tool_name": "Read", "tool_input": {"file_path": "ruff.toml"}},
    ),
    (
        "config_protection._PACKAGE_JSON_LINT_KEYS",
        lambda item: _write_content("package.json", '{"' + item + '": {}}'),
        _write_content("package.json", '{"name": "x"}'),
    ),
    (
        "config_protection._TOX_COMMAND_KEYS",
        lambda item: _write_content("tox.ini", f"{item} = pytest\n"),
        _write_content("tox.ini", "[tox]\nenvlist = py312\n"),
    ),
    (
        "config_protection._PROTECTED_PATH_SEGMENTS",
        lambda item: _write("/".join(item) + "/pre-commit"),
        _write("docs/hooks/pre-commit"),
    ),
    (
        # ``*`` は worktree 名。
        "config_protection._GIT_CONFIG_REPO_SEGMENTS",
        lambda item: _write_content("/".join(item).replace("*", "wt"), "[core]\n\thooksPath = /dev/null\n"),
        _write_content("docs/config", "[core]\n\thooksPath = /dev/null\n"),
    ),
    (
        # 畳み済みの複製は大文字綴りで引く（APFS / NTFS では同じファイル）。
        "config_protection._GIT_CONFIG_REPO_SEGMENTS_FOLDED",
        lambda item: _write_content("/".join(item).replace("*", "wt").upper(), "[core]\n\thooksPath = /dev/null\n"),
        _write_content("docs/config", "[core]\n\thooksPath = /dev/null\n"),
    ),
)

# 層1 をこのファイルの別テストが担っている語彙と、その担い手。
#
# 「層1 が無い」ことと「層1 を別の名前で持っている」ことを取り違えないための表。
# ここに載せるには、その語彙の**全要素**を実測しているテストが実在する必要がある。
_VOCABULARY_LAYER1_ELSEWHERE = {
    "bash_config_protection._BASH_TOOL_NAMES": "_HARNESS_CASES の copilot bash 危険 / 通常",
    "bash_config_protection._ALL_PROTECTED_BASENAMES": "test_every_protected_file_is_denied_on_bash_write",
    "bash_config_protection._DIRECTORY_CHANGE_COMMANDS": "test_every_directory_change_command_forces_unconditional_deny",
    "bash_config_protection._HOME_LOG_PATH_SEGMENTS": "test_tilde_form_is_blocked / test_absolute_form_is_blocked / test_relative_after_cd_is_blocked (TestHomeLogDirectoryProtection)",
    "host_guard_targets._HOST_SETTINGS_PATH_SEGMENTS": "test_tilde_form_is_blocked の settings-redirect / settings-local-append (TestHostGuardFilesOutsideRepo)",
    "config_protection.PROTECTED_FILES": "test_every_protected_file_is_denied_on_write",
    "config_protection.CONDITIONALLY_PROTECTED_FILES": "test_every_conditionally_protected_file_denies_only_lint_signals",
    "config_protection.PROTECTED_FILES_FOLDED": "test_folded_protected_sets_cover_every_declared_name",
    "config_protection.CONDITIONALLY_PROTECTED_FILES_FOLDED": "test_folded_protected_sets_cover_every_declared_name",
    "config_protection._PROTECTED_PATH_SEGMENTS_FOLDED": "_BLOCKED_CASES の保護 path の大文字（Bash）",
    "config_protection._LINT_SECTION_HEADERS": "test_every_lint_section_header_is_detected",
    "config_protection._LINT_KEYS": "test_every_lint_key_is_detected",
    "config_protection._SETTINGS_GUARD_KEYS": "test_every_conditionally_protected_file_denies_only_lint_signals の settings.json / settings.local.json",
}

# 層1 を持たない語彙と、その理由。
#
# 「要素を消しても判定が変わらない」ケースを層1 に混ぜると、緑が何も保証しない
# 偽の受領証になる。持てないものは持てないと書き、層2 の完全一致だけで守る。
_VOCABULARY_LAYER1_ABSENT = {
    "config_protection.GIT_CONFIG_DISPLAY_NAMES": (
        "派生値（`_GIT_CONFIG_REPO_SEGMENTS` の連結とホーム側の表示名の和集合）で、Bash 側の"
        "ブロック文言を選ぶ鍵にすぎない。保護対象かどうかの判定は変えない"
    ),
    "git_config_text.GLOBAL_GIT_CONFIG_DISPLAY_NAMES": (
        "表示名であり、保護対象のパスはホームと XDG_CONFIG_HOME から実行時に決まる。要素単位で判定が"
        "変わる経路が無い（パス判定は test_git_config_protection.TestProtectedPaths が担う）"
    ),
    "bash_config_protection._RAW_TEXT_RISK_INDICATORS": (
        "派生値（`_REMOVE_COMMANDS` 等の連結）であり、構成元の各集合が層1 を持つ。"
        "この tuple 自体は malformed JSON 専用の部分一致指標で、要素単位で判定が"
        "変わることを示す独立した経路が無い"
    ),
}


def _expand(cases: tuple[tuple[Any, ...], ...]) -> list[tuple[Any, ...]]:
    """語彙ごとのケース定義を、期待値の**全要素**へ展開する。

    展開元は必ず `_EXPECTED_VOCABULARIES`（チェックイン済みの期待値）にする。
    実装側の集合を入力にすると、要素を消したときに parametrize ケースも一緒に
    消えてスイートが緑のまま通る。

    Args:
        cases: 先頭要素が語彙の完全修飾名である定義タプルの列。

    Returns:
        ``(要素, *定義の残り)`` へ展開したケース列。

    Raises:
        例外は発生しません。
    """
    return [
        (qualified, item, *rest)
        for qualified, *rest in cases
        for item in sorted(_EXPECTED_VOCABULARIES[qualified], key=repr)
    ]


_DENY_ALLOW_PARAMS = _expand(_VOCABULARY_DENY_ALLOW_CASES)
_ALLOW_ONLY_PARAMS = _expand(_VOCABULARY_ALLOW_ONLY_CASES)
_WRITE_PARAMS = _expand(_VOCABULARY_WRITE_CASES)


@pytest.mark.parametrize(
    ("qualified", "item", "hook", "deny_template", "allow_template"),
    _DENY_ALLOW_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _DENY_ALLOW_PARAMS],
)
def test_every_vocabulary_element_changes_the_verdict(
    qualified: str,
    item: str,
    hook: str,
    deny_template: str,
    allow_template: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """語彙の全要素が、判定軸を外すと allow へ戻る形で deny を生むこと（層1）。

    deny 側だけでは「全部 deny する」実装でも緑になる。同じ語のまま判定軸
    （保護対象かどうか / in-place フラグ / 実行位置）だけを外した allow 側を
    対に置き、要素が**その軸で**効いていることを示す。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        hook: 対象フック名。
        deny_template: exit 2 になるコマンド書式。
        allow_template: exit 0 になるコマンド書式。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, _bash(_fill(deny_template, item)), monkeypatch) == 2, qualified
    assert _run(hook, _bash(_fill(allow_template, item)), monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    ("qualified", "item", "hook", "allow_template", "control_deny"),
    _ALLOW_ONLY_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _ALLOW_ONLY_PARAMS],
)
def test_every_parser_vocabulary_element_keeps_a_normal_command_allowed(
    qualified: str,
    item: str,
    hook: str,
    allow_template: str,
    control_deny: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解析語彙の全要素が、通常操作を allow に保つこと（層1・allow 側）。

    これらの語彙は要素を消すと「未知のオプション」になり、サブコマンド解決を
    信用できないとして fail-closed（deny）へ倒れる。したがって要素が効いている
    証拠は allow 側にしか現れない。同じフックが対照コマンドを deny することを
    同時に確認し、allow が空虚でないことを示す。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        hook: 対象フック名。
        allow_template: exit 0 になるコマンド書式。
        control_deny: 同じフックが exit 2 にする対照コマンド。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run(hook, _bash(control_deny), monkeypatch) == 2, qualified
    assert _run(hook, _bash(_fill(allow_template, item)), monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    ("qualified", "item", "build_deny", "allow_payload"),
    _WRITE_PARAMS,
    ids=[f"{case[0]}[{case[1]!r}]" for case in _WRITE_PARAMS],
)
def test_every_write_path_vocabulary_element_changes_the_verdict(
    qualified: str,
    item: Any,
    build_deny: Any,
    allow_payload: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Write 経路の語彙の全要素が deny を生み、対照が allow のままであること（層1）。

    Args:
        qualified: 語彙の完全修飾名（失敗時の可読性のため）。
        item: 語彙要素。
        build_deny: 要素から deny payload を組み立てる callable。
        allow_payload: 同じ検査軸を外した allow payload。
        monkeypatch: pytest の monkeypatch フィクスチャ。
    """
    assert _run("config_protection", build_deny(item), monkeypatch) == 2, qualified
    assert _run("config_protection", allow_payload, monkeypatch) == 0, qualified


@pytest.mark.parametrize(
    "operator", sorted(_EXPECTED_VOCABULARIES["bash_config_protection._REDIRECT_OPERATORS"])
)
def test_every_redirect_operator_yields_a_write_target(operator: str) -> None:
    """`_REDIRECT_OPERATORS` の全要素がリダイレクト先を返すこと（層1・単体）。

    ここだけ end-to-end ではなく抽出関数を直接呼ぶ。``1>`` / ``2>`` /
    ``1>>`` / ``2>>`` は `tokenize` が数字を別トークンへ割るため ``>`` /
    ``>>`` そのものにも一致し、フックへ流す形では要素を消しても exit code が
    変わらない（＝緑が何も保証しない偽の受領証になる）。抽出関数の戻り値を
    見れば、要素ごとに参照されていることを実測できる。

    Args:
        operator: リダイレクト演算子。
    """
    assert bash_config_protection._redirect_targets(["printf", "x", operator, "ruff.toml"]) == [
        "ruff.toml"
    ]


@pytest.mark.parametrize(
    "operator",
    sorted(_EXPECTED_VOCABULARIES["commit_risk._MUTATING_REDIRECT_OPERATORS"]),
)
def test_every_mutating_redirect_operator_marks_the_segment(operator: str) -> None:
    """`_MUTATING_REDIRECT_OPERATORS` の全要素が mutation 判定を立てること（層1・単体）。

    `_REDIRECT_OPERATORS` と同じ理由で単体呼び出しにする。演算子を含まない
    同形のセグメントが False であることを対照に置き、判定が演算子由来だと示す。

    Args:
        operator: リダイレクト演算子。
    """
    assert commit_risk._segment_mutates_worktree_or_index(
        ["printf", "x", operator, "app.py"]
    )
    assert not commit_risk._segment_mutates_worktree_or_index(["printf", "x", "app.py"])


@pytest.mark.parametrize(
    "suffix", _EXPECTED_VOCABULARIES["shell_tokens._HEREDOC_CONTINUATION_SUFFIXES"]
)
def test_every_heredoc_continuation_suffix_keeps_the_body(suffix: str) -> None:
    """`_HEREDOC_CONTINUATION_SUFFIXES` の全要素が本文の剥がしを止めること（層1・単体）。

    docs/adr/shell-analysis-boundary.md の「除去しない条件 2」。継続演算子で終わる演算子行は本文の開始位置が
    次行とは限らないため、剥がさず検出側へ倒す。

    フックへ流す形は使えない。``\\`` で終わる行は `shlex` が改行ごとエスケープして
    次行の先頭語と 1 トークンへ融合させるため（実測: ``\\ngit`` という 1 トークンに
    なり git 起動として認識されない）、本文を残しても exit code が変わらない。
    正規化関数の出力を直接見る。

    Args:
        suffix: 継続演算子。
    """
    body = _NO_VERIFY
    kept = f"cat <<'EOF' {suffix}\n{body}\nEOF"
    stripped = f"cat <<'EOF'\n{body}\nEOF"
    assert shell_tokens.strip_data_heredoc_bodies(kept) == kept
    assert shell_tokens.strip_data_heredoc_bodies(stripped) != stripped


@pytest.mark.parametrize(
    "suffix", sorted(_EXPECTED_VOCABULARIES["commit_quality_scanner._LINTABLE_SUFFIXES"])
)
def test_every_lintable_suffix_is_checked(suffix: str) -> None:
    """`_LINTABLE_SUFFIXES` の全要素が lint 検査対象になること（層1・単体）。

    Args:
        suffix: 拡張子。
    """
    assert commit_quality_scanner.should_lint_file(f"src/app{suffix}")
    assert not commit_quality_scanner.should_lint_file("src/app.txt")


@pytest.mark.parametrize(
    "name", sorted(_EXPECTED_VOCABULARIES["commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES"])
)
def test_every_excluded_lockfile_skips_secret_scan(name: str) -> None:
    """`_SECRET_SCAN_EXCLUDED_FILENAMES` の全要素が secret 走査から外れること（層1・単体）。

    Args:
        name: ロックファイル名。
    """
    assert not commit_quality_scanner.should_scan_secrets(f"sub/{name}")
    assert commit_quality_scanner.should_scan_secrets("sub/app.py")


@pytest.mark.parametrize(
    "suffix", _EXPECTED_VOCABULARIES["commit_quality_scanner._MINIFIED_SUFFIXES"]
)
def test_every_minified_suffix_skips_secret_scan(suffix: str) -> None:
    """`_MINIFIED_SUFFIXES` の全要素が secret 走査から外れること（層1・単体）。

    Args:
        suffix: 圧縮生成物の拡張子。
    """
    assert not commit_quality_scanner.should_scan_secrets(f"dist/bundle{suffix}")
    assert commit_quality_scanner.should_scan_secrets("dist/bundle.js")


def test_every_vocabulary_has_a_declared_layer1_status() -> None:
    """層2 で固定した全語彙が、層1 の担い手か不在理由のどちらかを宣言していること。

    層1 は「集合には載っているが実際には効いていない」要素を検出する層で、
    書けるのに書いていない集合を静かに増やさないための宣言を要求する。宣言先は
    次の 4 つのいずれか: この表の deny/allow 対、allow のみ対、Write 経路対、
    単体呼び出し。それ以外は `_VOCABULARY_LAYER1_ELSEWHERE`（別テストが担う）か
    `_VOCABULARY_LAYER1_ABSENT`（理由付きで持たない）へ明示する。
    """
    unit_covered = {
        "bash_config_protection._REDIRECT_OPERATORS",
        "commit_risk._MUTATING_REDIRECT_OPERATORS",
        "shell_tokens._HEREDOC_CONTINUATION_SUFFIXES",
        "commit_quality_scanner._LINTABLE_SUFFIXES",
        "commit_quality_scanner._SECRET_SCAN_EXCLUDED_FILENAMES",
        "commit_quality_scanner._MINIFIED_SUFFIXES",
    }
    declared = (
        {case[0] for case in _VOCABULARY_DENY_ALLOW_CASES}
        | {case[0] for case in _VOCABULARY_ALLOW_ONLY_CASES}
        | {case[0] for case in _VOCABULARY_WRITE_CASES}
        | unit_covered
        | set(_VOCABULARY_LAYER1_ELSEWHERE)
        | set(_VOCABULARY_LAYER1_ABSENT)
    )
    assert declared == set(_EXPECTED_VOCABULARIES)


@pytest.mark.parametrize(
    "command",
    [
        "GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -v GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -C /tmp GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "command -p env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "sudo -E GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "nohup env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "timeout 5 env GIT_CONFIG_GLOBAL=/tmp/e git commit -m x",
        "env -v GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=/tmp git commit -m x",
        "HOME=/tmp/h git commit -m x",
        "env HOME=/tmp/h git commit -m x",
        "env -i HOME=/tmp/h git commit -m x",
        "/usr/bin/env HOME=/tmp/h git commit -m x",
        "env -C /tmp XDG_CONFIG_HOME=/tmp/h git commit -m x",
        "sudo -E HOME=/tmp/h git commit -m x",
    ],
)
def test_env_injection_is_denied_through_any_wrapper(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """前置にラッパやオプションが何個あっても env 注入を deny すること。

    ラッパ名とそのオプションを列挙して読み飛ばす設計では、列挙に無い形
    （下の 5 形）で走査が途中で止まって素通りする。前置トークンを全部走査する
    形の回帰防止。
    """
    assert _run("block_no_verify", _bash(command), monkeypatch) == 2


@pytest.mark.parametrize(
    "command",
    [
        "HOME=/tmp/h grep -rn git .",
        "GIT_CONFIG_GLOBAL=/tmp/e grep -rn git .",
    ],
)
def test_env_assignment_before_non_git_command_naming_git_is_denied(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git 以外のコマンドの引数に `git` があるだけでも前置代入は deny される（受容済みの誤検出）。

    git 起動の判定はトークンの basename が `git` であることだけを見るため、`grep -rn git .` の
    引数 `git` も git 起動として数える。回避は代入を外すか別セグメントに分けること。
    """
    assert _run("block_no_verify", _bash(command), monkeypatch) == 2


@pytest.mark.parametrize(
    "command",
    [
        'git commit -m "a=b"',
        "git commit --author=Foo -m x",
        "env SOME_VAR=x git commit -m y",
    ],
)
def test_benign_assignments_stay_allowed(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """危険でない代入・git のオプションは通り続けること（拾いすぎの対照）。

    前置を全部走査しても、deny するのは危険な変数名に一致したときだけ。
    git のオプション（`--author=Foo`）は識別子で始まらないので代入に一致しない。
    """
    assert _run("block_no_verify", _bash(command), monkeypatch) == 0


def test_huge_single_token_is_blocked_before_tokenizing(monkeypatch: pytest.MonkeyPatch) -> None:
    """巨大な単一トークンがトークン予算を素通りせず BLOCK されること。

    `MAX_COMMAND_TOKENS` はトークン**数**しか縛らないが、走査コストは `shlex` の
    **バイト数**の二次で決まる。1MB の単一トークンはトークン数 2 なので予算を
    素通りすると、実測 12.71 秒（timeout 15 秒に対し余裕 15%）かかる。host が
    kill した hook は exit code を返さないため silent fail-open になる。
    """
    from claq.hooks.shell_tokens import MAX_COMMAND_BYTES, command_exceeds_scan_budget

    huge = "echo " + "A" * (MAX_COMMAND_BYTES + 1)

    assert command_exceeds_scan_budget(huge) is True
    assert _run("block_no_verify", _bash(huge), monkeypatch) == 2
    # 上限の内側は通る（拾いすぎの対照）
    assert command_exceeds_scan_budget("echo " + "A" * 1000) is False


def test_scan_budget_is_measured_in_bytes_not_characters() -> None:
    """マルチバイト文字でもバイト数で判定すること。

    文字数で数えると、日本語 1 文字 = 3 バイトの入力が上限の 3 倍まで通り、
    走査コストは 9 倍になる。
    """
    from claq.hooks.shell_tokens import MAX_COMMAND_BYTES, command_exceeds_scan_budget

    multibyte = "あ" * (MAX_COMMAND_BYTES // 3 + 1)

    assert len(multibyte) < MAX_COMMAND_BYTES
    assert command_exceeds_scan_budget(multibyte) is True


def test_oversized_command_is_blocked_by_bash_config_protection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """トークン予算を超えるコマンドは走査せず BLOCKED にすること。

    `bash_config_protection` だけが予算を見ていなかった。40 万トークンの
    ``touch a a a ...; rm ruff.toml`` は走査に 20.7 秒かかり、`hooks.json` の timeout
    （15 秒）で host に kill される。kill された hook は exit code を返さないため
    silent fail-open になる。走査前に弾けば同じ入力が即座に exit 2 になる。
    """
    from claq.hooks.shell_tokens import MAX_COMMAND_TOKENS

    oversized = "echo " + "a " * (MAX_COMMAND_TOKENS + 1)

    assert _run("bash_config_protection", _bash(oversized), monkeypatch) == 2
    assert "exceeds" in capsys.readouterr().err


def test_oversized_command_inside_sh_c_is_blocked_by_bash_config_protection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``sh -c '<巨大なコマンド>'`` の再帰境界にも同じ予算が効くこと。

    入口だけに置いた予算は、外側が数トークンしか無いネスト payload を通し、再帰先が
    無予算で走る。
    """
    from claq.hooks.shell_tokens import MAX_COMMAND_TOKENS

    inner = "echo " + "a " * (MAX_COMMAND_TOKENS + 1)

    assert _run("bash_config_protection", _bash(f"sh -c '{inner}'"), monkeypatch) == 2
    assert "exceeds" in capsys.readouterr().err
    # 予算内のネストは中身で判定される（予算が過剰に効かない）。
    assert _run("bash_config_protection", _bash("sh -c 'printf x > ruff.toml'"), monkeypatch) == 2
    assert _run("bash_config_protection", _bash("sh -c 'printf x > notes.txt'"), monkeypatch) == 0
