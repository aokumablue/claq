"""内容次第で保護するファイルの検査（`config_protection` が書込み先ごとに呼ぶ）。

ファイル名だけでは塞げない設定を、書込みの内容で判定する。

- lint 系の汎用設定（``pyproject.toml`` 等）: ディスク上の所属セクションの解決（主）とテキスト照合（副）
- ホスト設定（``settings.json`` 等）: 置換前後の全文を JSON として比べる
- git config: 置換前後の全文を解析して比べる

フックの入口ではなく、どのファイルを検査するかは呼び出し側が決める。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NoReturn

from claq.hooks.block_no_verify import is_sensitive_config_key
from claq.hooks.git_config_text import (
    GitConfigParseError,
    adds_sensitive_entries,
    git_config_text_after,
    read_current_text,
)
from claq.hooks.hook_common import memoized_in_scan, normalize_protected_name
from claq.hooks.stdin_io import MAX_STDIN_BYTES
from claq.lib.harness import normalize_tool_name

# セクション見出しの照合パターン（前方一致）。[tool.ruff.lint] のような
# サブセクションも拾うため prefix 一致にする。`[testenv` は tox.ini の
# `[testenv]` / `[testenv:py312]` 等のサブ環境セクションを両方カバーする。
_LINT_SECTION_HEADERS = (
    "[tool.ruff",
    "[tool.coverage",
    "[tool.pytest",
    "[flake8]",
    "[mypy]",
    "[pycodestyle]",
    "[testenv",
)

# セクション見出しを伴わない値行編集（`fail_under = 100` → `80`）を拾うためのキー照合。
#
# 行頭の空白は ``[ \t]`` に限る。``\s`` は改行を含むため、各行頭から残りの空白全体を
# 取って後退し、改行だけの入力で二次オーダーになる。hooks.json の timeout を超えて host
# にフックを殺されると allow へ倒れ、保護そのものが無効化される。
_LINT_KEYS = ("ignore", "select", "per-file-ignores", "exclude", "fail_under", "addopts")
_LINT_KEY_PATTERN = re.compile(
    r"(?m)^[ \t]*(" + "|".join(re.escape(key) for key in _LINT_KEYS) + r")[ \t]*="
)

# ホスト設定（settings.json / settings.local.json）で置換前後を比べる top-level の部分木。
# 有無の変化も変化として扱う。`disableAllHooks` は全フックを一括で止める。
_SETTINGS_GUARDED_SUBTREES = ("hooks", "enabledPlugins", "disabledPlugins", "disableAllHooks")

# 同じく比べる `env` のキー。`CLAQ_PYTHON` は `runtime/claq-hook` が exec し、`PATH` は
# `CLAQ_PYTHON` 未設定時の `python3` 探索を、`PYTHONPATH` / `PYTHONHOME` は標準ライブラリの解決を
# 左右するため、どれも全ツール呼び出しでの任意コード実行になる。Windows の環境変数名は大小を
# 区別しないので、照合時に casefold して比べる。
_SETTINGS_GUARDED_ENV_NAMES = ("CLAQ_PYTHON", "PATH", "PYTHONPATH", "PYTHONHOME")

# package.json は TOML/INI ではないため専用のキー照合にする。
_PACKAGE_JSON_LINT_KEYS = ("eslintConfig", "prettier")

# tox.ini の実行コマンドキー。`commands` は他のファイルにも現れうる汎用的な語なので、
# `_LINT_KEYS` へは足さず tox.ini 限定にする。行頭空白を ``[ \t]`` に絞る理由は
# `_LINT_KEY_PATTERN` と同じ。
_TOX_COMMAND_KEYS = ("commands", "commands_pre", "commands_post")
_TOX_COMMAND_KEY_PATTERN = re.compile(
    r"(?m)^[ \t]*(" + "|".join(re.escape(key) for key in _TOX_COMMAND_KEYS) + r")[ \t]*="
)

# git config・ホスト設定の置換前後の全文の上限。置換前はディスクから読むので stdin の上限が効かず、
# 置換後は MultiEdit の replace_all を重ねると倍々に伸びる（`a`→`aa` を 20 回で 1 KiB が 1 GiB）。
# 超えたら検査できないとして deny する（hook が host timeout で kill されると許可へ倒れるため）。
_FULL_TEXT_MAX_BYTES = 256 * 1024


def _editable_text(tool_name: str, container: Any) -> list[str] | None:
    """書込み系入力から検査対象テキスト（新規/変更内容）の断片リストを取り出す。

    apply_patch はパッチ本文全体、Write は content、Edit/search_replace は
    old_string+new_string、MultiEdit は edits[].old_string+new_string を
    それぞれ 1 断片として返す。apply_patch は normalize_tool_name で
    "Edit" に正規化されてしまうため、判定は正規化前の生ツール名で行う。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        検査対象テキストの断片リスト。1 つも取り出せなければ None
        （呼び出し側は fail-closed として扱うこと）。

    Raises:
        例外は発生しません。
    """
    if tool_name == "apply_patch":
        text = container.get("input") if isinstance(container, dict) else container
        return [text] if isinstance(text, str) else None

    if not isinstance(container, dict):
        return None

    normalized = normalize_tool_name(tool_name).lower()
    if normalized == "write":
        content = container.get("content")
        return [content] if isinstance(content, str) else None

    # MultiEdit は edits[]、edit / search_replace 等はコンテナ自身を 1 つの Edit 相当として扱う。
    edits = container.get("edits") if normalized == "multiedit" else [container]
    if not isinstance(edits, list):
        return None
    snippets = [
        snippet
        for edit in edits
        if isinstance(edit, dict)
        for key in ("old_string", "new_string")
        if isinstance(snippet := edit.get(key), str)
    ]
    return snippets or None


def _section_header(line: str) -> str | None:
    """行がセクション見出しなら、前後の空白を除いた見出しを返す。

    Args:
        line: 1 行、または行の先頭から途中までの断片。

    Returns:
        ``[`` で始まり ``]`` で終わるなら見出し、それ以外は None。

    Raises:
        例外は発生しません。
    """
    stripped = line.strip()
    return stripped if stripped.startswith("[") and stripped.endswith("]") else None


@memoized_in_scan
def _read_section_index(file_path: str) -> tuple[str, list[int], list[str | None]] | None:
    """`file_path` をディスクから読み、行ごとの開始位置とその行より前の最後の見出しを返す。

    断片ごとに読み直して行を数えると、MultiEdit の断片（編集件数の 2 倍）の数だけ全行を回り、上限内の
    624 KB の payload で 22.9 秒かかって timeout（15 秒）を超えた（kill された hook は host に許可として
    扱われる）。`scan_memo` の間は 1 回だけ読み、断片ごとの判定を二分探索にする。

    Args:
        file_path: 対象ファイルパス。

    Returns:
        (読んだ本文, 各行の開始位置, 各行より前で最後に現れた見出し)。読めない・見つからない場合は None。

    Raises:
        例外は発生しません。
    """
    try:
        path = Path(file_path)
        if not path.is_file():
            return None
        # 実ファイルはエージェント自身が任意サイズへ膨らませられ、無制限に読むと timeout で
        # フックが殺され allow へ倒れる。見出しとキーは先頭付近に現れるので先頭だけで足りる。
        with path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_STDIN_BYTES)
    except OSError:
        return None
    line_starts: list[int] = []
    headers_before: list[str | None] = []
    offset = 0
    header: str | None = None
    for line in text.splitlines(keepends=True):
        line_starts.append(offset)
        headers_before.append(header)
        offset += len(line)
        header = _section_header(line) or header
    return text, line_starts, headers_before


def _resolve_lint_section_from_disk(file_path: str, snippet: str) -> bool | None:
    """`file_path` をディスクから読み、`snippet` が属するセクションが lint 系か判定する。

    値行だけの Edit（見出しが old_string/new_string に含まれない）でも、ディスク上の
    現在の内容から所属セクションを解決する（主判定）。Write の content はディスクに
    まだ無いため多くは判定不能になり、副判定（`_text_has_protected_signal`）が効く。

    Args:
        file_path: 対象ファイルパス。
        snippet: 所属セクションを調べたいテキスト断片。

    Returns:
        True: lint 系セクションに属すると判定できた。
        False: 読めた上で lint 系セクションではないと判定できた。
        None: 読めない・見つからない等、判定不能。

    Raises:
        例外は発生しません。
    """
    if not snippet:
        return None
    section_index = _read_section_index(file_path)
    if section_index is None:
        return None
    text, line_starts, headers_before = section_index
    index = text.find(snippet)
    if index < 0:
        return None
    # 全 hook の起動に載せないよう（import で約 0.3 ms）、ここへ到達したときだけ読み込む。
    from bisect import bisect_right

    line = bisect_right(line_starts, index) - 1
    # 断片の手前で切れた行も見る（``[tool.ruff] # x`` の ``# x`` は ``[tool.ruff]`` に属する）。
    header = _section_header(text[line_starts[line] : index]) or headers_before[line]
    if header is None:
        return False
    return any(header.startswith(candidate) for candidate in _LINT_SECTION_HEADERS)


def _text_has_protected_signal(text: str, file_name: str) -> bool:
    """テキストに保護対象を弱めうる兆候が含まれるか判定する（副判定）。

    package.json は専用のキーを、tox.ini は実行コマンドのキー（見出しを伴わない値行編集でも
    検出するため）を、それ以外は共通のセクション見出しの部分一致またはキー行照合
    （`ignore = [...]` 等）を見る。テキストは stdin の payload から来るので、長さは stdin の上限
    （超えたら deny）で抑えられている（1 MiB の走査で 25 ms 程度。線形）。

    Args:
        text: 検査対象テキスト。
        file_name: 判定対象のファイル名（basename）。

    Returns:
        lint/format/coverage 設定を弱めうる兆候があれば True。

    Raises:
        例外は発生しません。
    """
    # ファイル別分岐も畳んだ名前で選ぶ（``Package.json`` を専用のキー照合へ落とすため）。
    folded_name = normalize_protected_name(file_name)
    if folded_name == "package.json":
        return any(key in text for key in _PACKAGE_JSON_LINT_KEYS)
    if folded_name == "tox.ini" and _TOX_COMMAND_KEY_PATTERN.search(text):
        return True
    if any(header in text for header in _LINT_SECTION_HEADERS):
        return True
    return bool(_LINT_KEY_PATTERN.search(text))


def _conditional_blocked_message(file_name: str) -> str:
    """条件付き保護ファイルの lint 設定編集に対するブロック理由を生成する。

    Args:
        file_name: ブロックされたファイル名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: The change to {file_name} appears to touch a protected setting "
        "(a lint/format/coverage section or key). "
        "Fix the source code to satisfy the rules instead of weakening the config. "
        "If this change is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _unverifiable_conditional_message(file_name: str) -> str:
    """条件付き保護ファイルの変更内容が一切取得できない場合のブロック理由を生成する。

    Args:
        file_name: ブロックされたファイル名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Could not determine the content of the change to {file_name} "
        "(a file that may contain protected configuration: lint/format/coverage settings or "
        "git hook keys). Refusing an unverifiable write rather than risking a silent config weakening."
    )


def _reject_json_constant(name: str) -> NoReturn:
    """JSON の ``NaN`` / ``Infinity`` / ``-Infinity`` を拒否する。

    host の JSON パーサはこれらを受け付けずファイルごと無視するため、置換後に現れると
    ``enabledPlugins`` ごと設定が消えうる。読めない JSON として扱う。

    Args:
        name: パーサが渡す定数名。

    Raises:
        ValueError: 常に送出する。
    """
    raise ValueError(f"non-standard JSON constant: {name}")


def _parse_settings_object(text: str) -> dict[str, Any] | None:
    """ホスト設定の全文を JSON object として読む。

    深すぎる入れ子の RecursionError はここでは捕まえず、比較と合わせて呼び出し側が deny にする。

    Args:
        text: ファイルの全文。

    Returns:
        読めた object。JSON として読めない・top-level が object でない場合は None。

    Raises:
        RecursionError: 入れ子が深すぎる。
    """
    try:
        value = json.loads(text, parse_constant=_reject_json_constant)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _settings_guarded_view(settings: dict[str, Any]) -> dict[str, Any]:
    """ホスト設定のうち、保護フックを止めうる部分だけを取り出す。

    ``env`` が無いことは空 object と同じに扱う（保護対象の名前を持たない ``env`` を消す編集を
    止めないため）。``env`` が object でなければ、名前で選べないので値全体を比べる。

    Args:
        settings: 解析済みのホスト設定。

    Returns:
        ``_SETTINGS_GUARDED_SUBTREES`` の部分木（存在するもののみ）と ``env`` の保護対象の名前。

    Raises:
        例外は発生しません。
    """
    view = {key: settings[key] for key in _SETTINGS_GUARDED_SUBTREES if key in settings}
    env = settings.get("env", {})
    guarded_names = {name.casefold() for name in _SETTINGS_GUARDED_ENV_NAMES}
    view["env"] = (
        {name: value for name, value in env.items() if name.casefold() in guarded_names}
        if isinstance(env, dict)
        else env
    )
    return view


def _settings_blocked_message(file_name: str) -> str:
    """ホスト設定の変更が保護フックを止めうる場合のブロック理由を生成する。

    Args:
        file_name: ブロックされたファイル名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: The change to {file_name} changes a setting that can disable the protection hooks "
        "(hooks, enabledPlugins/disabledPlugins, env CLAQ_PYTHON/PATH/PYTHONPATH/PYTHONHOME). "
        "Other settings (model, permissions, other env entries) can be changed freely. "
        "If this change is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _settings_unverifiable_message(file_name: str) -> str:
    """ホスト設定の変更結果を検証できない場合のブロック理由を生成する。

    Args:
        file_name: ブロックされたファイル名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: The change to {file_name} could not be verified: the result must be a plain JSON object "
        "(no comments, trailing commas, NaN/Infinity, or excessive nesting) within "
        f"{_FULL_TEXT_MAX_BYTES} bytes, built from a Write or an exact Edit/MultiEdit. "
        "Ask the user to make this change instead of working around the check."
    )


def _settings_block_reason(tool_name: str, container: Any, file_path: str, file_name: str) -> str | None:
    """ホスト設定への Edit/Write を、置換前後の全文を JSON として比べて検査する。

    ``_settings_guarded_view`` が変わる編集だけを deny する。置換前が読めない（不在・壊れている・
    object でない）ときは保護対象が無かったものとして扱う。置換後が読めない・全文を作れない・
    走査上限を超える場合は検査できないので deny する。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        file_path: 対象ファイルパス。
        file_name: 対象ファイルの basename。

    Returns:
        ブロック理由。問題なければ None。

    Raises:
        例外は発生しません。
    """
    before = read_current_text(file_path, _FULL_TEXT_MAX_BYTES + 1)
    if len(before) > _FULL_TEXT_MAX_BYTES:
        return _settings_unverifiable_message(file_name)
    after = git_config_text_after(tool_name, container, before, _FULL_TEXT_MAX_BYTES)
    if after is None or len(after) > _FULL_TEXT_MAX_BYTES:
        return _settings_unverifiable_message(file_name)
    # 深い入れ子は解析・直列化で RecursionError になる。貫通すると exit 1（PreToolUse では allow）。
    try:
        after_obj = _parse_settings_object(after)
        if after_obj is None:
            return _settings_unverifiable_message(file_name)
        before_obj = _parse_settings_object(before) or {}
        # 直列化して比べる。dict の == は true と 1、1 と 1.0 を同じとみなし、型の書換えを見逃す。
        before_view = json.dumps(_settings_guarded_view(before_obj), sort_keys=True)
        after_view = json.dumps(_settings_guarded_view(after_obj), sort_keys=True)
        if before_view != after_view:
            return _settings_blocked_message(file_name)
    except RecursionError:
        return _settings_unverifiable_message(file_name)
    return None


def conditional_block_reason(tool_name: str, container: Any, file_path: str, file_name: str) -> str | None:
    """`config_protection.CONDITIONALLY_PROTECTED_FILES` に該当するファイルへの変更を検査する。

    ホスト設定は `_settings_block_reason` で全文を比べる。それ以外は主（ディスク上の
    セクション解決）・副（テキスト照合）の和集合で行い、内容が一切取得できない場合は
    fail-closed で deny する。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        file_path: 対象ファイルパス。
        file_name: 対象ファイルの basename。

    Returns:
        ブロック理由。問題なければ None。

    Raises:
        例外は発生しません。
    """
    if normalize_protected_name(file_name) in ("settings.json", "settings.local.json"):
        return _settings_block_reason(tool_name, container, file_path, file_name)
    snippets = _editable_text(tool_name, container)
    if snippets is None:
        return _unverifiable_conditional_message(file_name)

    in_lint_section = any(_resolve_lint_section_from_disk(file_path, snippet) is True for snippet in snippets)
    if in_lint_section or _text_has_protected_signal("\n".join(snippets), file_name):
        return _conditional_blocked_message(file_name)
    return None


def _git_config_blocked_message(display_name: str) -> str:
    """git config への書込みが hooksPath 等を新しく持ち込む場合のブロック理由を生成する。

    Args:
        display_name: ブロックされたパスの表示名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: The change to {display_name} adds or changes a key that can disable git hooks "
        "(core.hooksPath, include.path / includeIf, any alias). Ordinary settings (remote, branch, "
        "user) can be changed freely, but a key that runs commands cannot be added here: "
        "if this change is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _git_config_unparsable_message(display_name: str) -> str:
    """git config への書込み内容を検証できない場合のブロック理由を生成する。

    Args:
        display_name: ブロックされたパスの表示名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: The change to {display_name} could not be verified: the resulting file does not "
        f"parse as git config text, or the file or the result exceeds {_FULL_TEXT_MAX_BYTES} bytes. "
        "Refusing an unverifiable write rather than risking a git-hook bypass."
    )


def git_config_block_reason(tool_name: str, container: Any, file_path: str, display_name: str) -> str | None:
    """git config への Edit/Write を、置換前後の全文を比べて検査する。

    置換後の全文を解析し、フック実行を左右する key（`is_sensitive_config_key`）が置換前に
    無かった、または値が変わったときに deny する。既存の ``[alias]`` の内側へ足す断片も、
    ディスク上の全文から所属セクションが決まるため検出できる。全文を作れない・解析できない・
    走査上限を超える場合も deny する。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。
        file_path: 対象ファイルパス。
        display_name: 対象の表示名（``.git/config`` 等）。

    Returns:
        ブロック理由。問題なければ None。

    Raises:
        例外は発生しません。
    """
    before = read_current_text(file_path, _FULL_TEXT_MAX_BYTES + 1)
    if len(before) > _FULL_TEXT_MAX_BYTES:
        return _git_config_unparsable_message(display_name)
    after = git_config_text_after(tool_name, container, before, _FULL_TEXT_MAX_BYTES)
    if after is None:
        return _unverifiable_conditional_message(display_name)
    if len(after) > _FULL_TEXT_MAX_BYTES:
        return _git_config_unparsable_message(display_name)
    try:
        if adds_sensitive_entries(before, after, is_sensitive_config_key):
            return _git_config_blocked_message(display_name)
    except GitConfigParseError:
        return _git_config_unparsable_message(display_name)
    return None
