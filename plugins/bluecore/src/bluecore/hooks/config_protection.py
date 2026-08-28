"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック。emit_block_output が stderr の理由と
      stdout の permissionDecision: deny JSON を同時に出す)

入力切り捨て時のブロックは本モジュール自身が判定する（launcher はインプロ
セスで直接ターゲットを実行するだけで stdin を代読しないため）。ファイル名
の保護判定と合わせ自己完結させている。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from bluecore.hooks.hook_common import (
    MAX_STDIN_BYTES,
    basename,
    emit_block_output,
    parse_json_object,
    read_raw_stdin_with_truncation,
    resolve_effective_target,
)
from bluecore.lib.harness import (
    INPUT_CONTAINER_KEYS,
    extract_file_paths,
    extract_raw_tool_name,
    extract_tool_input,
    normalize_tool_name,
)

# matcher（hooks.json）は Edit/Write/MultiEdit 系のエイリアスにアンカーされた
# 正規表現で、書込み系ツールに限定されている（全ツールではない）。それでも
# 正規化後の値をここで再度絞るのは、matcher の綴りが将来ズレても本体側で
# 書込み系以外を確実に早期 return するための多重防御。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

# apply_patch のパッチがパース不能なときの fail-closed 理由。
_UNPARSEABLE_PATCH_MESSAGE = "BLOCKED: Could not determine target files from patch input."

PROTECTED_FILES = {
    ".eslintrc",
    ".eslintrc.js",
    ".eslintrc.cjs",
    ".eslintrc.json",
    ".eslintrc.yml",
    ".eslintrc.yaml",
    "eslint.config.js",
    "eslint.config.mjs",
    "eslint.config.cjs",
    "eslint.config.ts",
    "eslint.config.mts",
    "eslint.config.cts",
    ".prettierrc",
    ".prettierrc.js",
    ".prettierrc.cjs",
    ".prettierrc.json",
    ".prettierrc.yml",
    ".prettierrc.yaml",
    "prettier.config.js",
    "prettier.config.cjs",
    "prettier.config.mjs",
    "biome.json",
    "biome.jsonc",
    ".ruff.toml",
    "ruff.toml",
    ".shellcheckrc",
    ".stylelintrc",
    ".stylelintrc.json",
    ".stylelintrc.yml",
    ".stylelintrc.yaml",
    ".markdownlint.json",
    ".markdownlint.yaml",
    ".markdownlint.yml",
    ".markdownlintrc",
    # コミット前検査そのものの定義。弱めれば lint 設定を弱めるのと同じ効果を
    # 得られるため、個別の lint 設定と同じ重みで保護する。
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
}

# ディレクトリ単位で保護する path。basename だけでは判定できない
# （``.git/hooks/pre-commit`` の basename ``pre-commit`` を一律ブロックすると
# 無関係な同名ファイルを巻き込む）ため、実体 path の親を見る。
#
# ``.git/hooks/`` は git 自身のフック本体。``block_no_verify`` は
# ``core.hooksPath`` の差し替えと ``--no-verify`` を塞いでいるが、フック
# スクリプトを直接書き換えれば同じ結果（検査が走らない状態）になる。
# 片側だけ塞ぐと、塞いだ経路の存在が誤った安心になる。
_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"),)

# ファイル名だけでは保護できない汎用設定ファイル。version bump・依存追加
# 等の正当な編集が頻繁なため全面ブロックはしない。書き込み内容が
# lint/format/coverage 設定を弱めうる場合のみ _conditional_block_reason
# でブロックする（R-07）。
CONDITIONALLY_PROTECTED_FILES = {
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
}

# セクション見出しの照合パターン（前方一致）。[tool.ruff.lint] のような
# サブセクションも拾うため prefix 一致にする。`[testenv` は tox.ini の
# `[testenv]` / `[testenv:py312]` 等のサブ環境セクションを両方カバーする
# （A-04 対応）。
_LINT_SECTION_HEADERS = (
    "[tool.ruff",
    "[tool.coverage",
    "[tool.pytest",
    "[flake8]",
    "[mypy]",
    "[pycodestyle]",
    "[testenv",
)

# セクション見出しを伴わない値行編集（例: `fail_under = 100` → `80`）を
# 拾うためのキー照合。見出しの外形だけを見ると、既存 pyproject.toml の
# 値行への Edit（見出しが old_string/new_string に含まれない）を素通り
# させてしまうため、キー単体でも検出する。
_LINT_KEYS = ("ignore", "select", "per-file-ignores", "exclude", "fail_under", "addopts")
_LINT_KEY_PATTERN = re.compile(
    r"(?m)^\s*(" + "|".join(re.escape(key) for key in _LINT_KEYS) + r")\s*="
)

# package.json は TOML/INI ではないため専用のキー照合にする。
_PACKAGE_JSON_LINT_KEYS = ("eslintConfig", "prettier")

# tox.ini の実行コマンドキー。`_LINT_KEYS` へ追加しない理由: `commands` は
# tox.ini 以外（例: pyproject.toml の正当な設定）にも現れうる汎用的な語で
# あり、グローバル追加すると無関係な変更まで deny してしまう。そのため
# tox.ini 限定分岐として独立させる（A-04 対応、package.json の特例と同じ形）。
_TOX_COMMAND_KEYS = ("commands", "commands_pre", "commands_post")
_TOX_COMMAND_KEY_PATTERN = re.compile(
    r"(?m)^\s*(" + "|".join(re.escape(key) for key in _TOX_COMMAND_KEYS) + r")\s*="
)


def _protected_path_segment(file_path: str) -> str | None:
    """パスがディレクトリ単位の保護対象配下かを判定する。

    symlink は `resolve_effective_target` で解決してから判定する
    （`_effective_basename` と同じ理由。H-02）。解決不能なら生パスで判定する。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/hooks``）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    for segments in _PROTECTED_PATH_SEGMENTS:
        width = len(segments)
        # ファイル名自身は含めず、親ディレクトリ列だけを見る。
        for index in range(len(parts) - width):
            if parts[index : index + width] == segments:
                return "/".join(segments)
    return None


def _effective_basename(file_path: str) -> str:
    """symlink を解決した実体 basename を返す（H-02 対応）。

    ``alias -> pyproject.toml`` のような symlink 経由の書込みが、raw path の
    basename（``alias``）だけを見る判定をすり抜けていた。
    ``resolve_effective_target`` で実体 path を解決してから basename を取り、
    保護対象判定を実際の書込み先ファイルに対して行う。

    解決不能（壊れた・循環した symlink 等）な場合は raw path 自体の
    basename にフォールバックする（ADR-0001 の inspection-failure fail-open
    と、直接 path 指定の保護を両立させるため。解決不能を deny に倒すと
    symlink を一切使わない正当な編集まで巻き込む）。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        判定に使う basename。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    return resolved.name if resolved is not None else basename(file_path)


def blocked_message_for_file(file_name: str) -> str:
    """保護されたファイルに対するブロックメッセージを生成する。

    Args:
        file_name: ブロックされたファイル名

    Returns:
        ブロックメッセージ文字列

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Modifying {file_name} is not allowed. "
        "Fix the source code to satisfy linter/formatter rules instead of "
        "weakening the config. If this is a legitimate config change, "
        "disable the config-protection hook temporarily."
    )


def _paths_from_container(tool_name: str, container: Any) -> list[str] | None:
    """1 つの入力コンテナから対象パスを取り出す。

    構造化パッチが判定不能なら None（呼び出し側は fail-closed）。
    file_path が無く dict なら旧 ``file`` キーを補完する。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        パス一覧。パッチ判定不能時は None。
    """
    file_paths = extract_file_paths(tool_name, container)
    if file_paths is None:
        return None
    if not file_paths and isinstance(container, dict):
        legacy = str(container.get("file") or "")
        if legacy:
            return [legacy]
    return file_paths


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
        if isinstance(container, str):
            return [container]
        if isinstance(container, dict):
            text = container.get("input")
            return [text] if isinstance(text, str) else None
        return None

    if not isinstance(container, dict):
        return None

    normalized = normalize_tool_name(tool_name).lower()
    if normalized == "write":
        content = container.get("content")
        return [content] if isinstance(content, str) else None

    if normalized == "multiedit":
        edits = container.get("edits")
        if not isinstance(edits, list):
            return None
        snippets = [
            edit.get(key)
            for edit in edits
            if isinstance(edit, dict)
            for key in ("old_string", "new_string")
        ]
        snippets = [s for s in snippets if isinstance(s, str)]
        return snippets or None

    # edit / search_replace 等はいずれも Edit 相当として扱う
    snippets = [s for s in (container.get("old_string"), container.get("new_string")) if isinstance(s, str)]
    return snippets or None


def _section_header_before(file_text: str, position: int) -> str | None:
    """`file_text` 内の `position`（文字インデックス）より前で最後に現れる `[...]` 見出しを返す。

    Args:
        file_text: ディスク上のファイル全文。
        position: 見出しを探す基準位置。

    Returns:
        直前のセクション見出し行（前後空白除去済み）。無ければ None。

    Raises:
        例外は発生しません。
    """
    header: str | None = None
    for line in file_text[:position].splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped
    return header


def _resolve_lint_section_from_disk(file_path: str, snippet: str) -> bool | None:
    """`file_path` をディスクから読み、`snippet` が属するセクションが lint 系か判定する。

    値行だけの Edit（`old_string`/`new_string` にセクション見出しが含まれ
    ない）でも、ディスク上の現在の内容から実際の所属セクションを解決する
    （二段構えの主判定。R-07）。Write の content は新規内容そのものが
    ディスクにまだ無いため、多くの場合ここでは判定不能（None）になり、
    副判定（`_text_has_lint_signal`）が本命として効く。

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
    try:
        path = Path(file_path)
        if not path.is_file():
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    index = text.find(snippet)
    if index < 0:
        return None
    header = _section_header_before(text, index)
    if header is None:
        return False
    return any(header.startswith(candidate) for candidate in _LINT_SECTION_HEADERS)


def _text_has_lint_signal(text: str, file_name: str) -> bool:
    """テキストに lint/format/coverage 関連のセクション見出しやキーが含まれるか判定する（副判定）。

    package.json は TOML/INI ではないため専用のキー照合を使う。tox.ini は
    `commands`/`commands_pre`/`commands_post` の実行コマンド変更を専用照合で
    拾う（A-04 対応。見出しを伴わない値行編集で `[testenv]` がスニペットに
    含まれない場合でも検出するため）。それ以外は共通のセクション見出しの
    部分一致、または見出しを伴わない値行編集を拾うためのキー行照合
    （`ignore = [...]` 等）で判定する。

    Args:
        text: 検査対象テキスト。
        file_name: 判定対象のファイル名（basename）。

    Returns:
        lint/format/coverage 設定を弱めうる兆候があれば True。

    Raises:
        例外は発生しません。
    """
    if file_name == "package.json":
        return any(key in text for key in _PACKAGE_JSON_LINT_KEYS)
    if file_name == "tox.ini" and _TOX_COMMAND_KEY_PATTERN.search(text):
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
        f"BLOCKED: The change to {file_name} appears to touch a lint/format/coverage "
        "configuration section or key. Fix the source code to satisfy linter/formatter "
        "rules instead of weakening the config. If this is a legitimate config change, "
        "disable the config-protection hook temporarily."
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
        "(a file that may contain lint/format/coverage configuration). Refusing an "
        "unverifiable write rather than risking a silent config weakening."
    )


def _conditional_block_reason(tool_name: str, container: Any, file_path: str, file_name: str) -> str | None:
    """`CONDITIONALLY_PROTECTED_FILES` に該当するファイルへの変更を検査する。

    ファイル名だけでは保護できない（version bump 等の正当な編集が頻繁な
    ため）。編集内容が lint/format/coverage 設定を弱めうる場合のみブロック
    する（R-07）。判定は主（ディスク上のセクション解決）・副（テキスト
    照合）の和集合で行う。内容が一切取得できない場合は fail-closed で
    deny する。

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
    snippets = _editable_text(tool_name, container)
    if snippets is None:
        return _unverifiable_conditional_message(file_name)

    for snippet in snippets:
        if _resolve_lint_section_from_disk(file_path, snippet) is True:
            return _conditional_blocked_message(file_name)

    if _text_has_lint_signal("\n".join(snippets), file_name):
        return _conditional_blocked_message(file_name)
    return None


def _block_reason_for_container(tool_name: str, container: Any) -> str | None:
    """1 つの入力コンテナが保護対象ならブロック理由を返す。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        ブロック理由。保護対象でなければ None。パッチ判定不能時は
        fail-closed メッセージ。

    Raises:
        例外は発生しません。
    """
    file_paths = _paths_from_container(tool_name, container)
    if file_paths is None:
        return _UNPARSEABLE_PATCH_MESSAGE
    for file_path in file_paths:
        protected_segment = _protected_path_segment(file_path)
        if protected_segment is not None:
            return blocked_message_for_file(f"{protected_segment}/")
        file_name = _effective_basename(file_path)
        if file_name in PROTECTED_FILES:
            return blocked_message_for_file(file_name)
        if file_name in CONDITIONALLY_PROTECTED_FILES:
            reason = _conditional_block_reason(tool_name, container, file_path, file_name)
            if reason:
                return reason
    return None


def _block_reason(data: dict[str, Any]) -> str | None:
    """書込み系入力が保護対象ならブロック理由を返す。

    Args:
        data: フック stdin の dict。

    Returns:
        ブロック理由。対象外・保護対象なしなら None。

    Raises:
        例外は発生しません。
    """
    tool_name = extract_raw_tool_name(data)
    if normalize_tool_name(tool_name).lower() not in _WRITE_TOOL_NAMES:
        return None
    for key in INPUT_CONTAINER_KEYS:
        if key not in data:
            continue
        reason = _block_reason_for_container(tool_name, extract_tool_input({key: data[key]}))
        if reason:
            return reason
    return None


def _truncation_blocked_message(max_bytes: int) -> str:
    """入力切り捨て時のブロック理由メッセージを生成する。

    切り捨てられたペイロードで保護判定をすり抜けさせないための guard。

    Args:
        max_bytes: 入力の最大バイト数。

    Returns:
        ブロック理由メッセージ。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Hook input exceeded {max_bytes} bytes for pre:config-protection. "
        "Refusing to bypass protection on a truncated payload. "
        "Retry with a smaller edit."
    )


_UNPARSEABLE_INPUT_MESSAGE = (
    "BLOCKED: Could not parse hook input for pre:config-protection. "
    "This hook only runs for write-tool calls (Edit/Write/MultiEdit), so a "
    "non-empty payload that fails to parse as JSON cannot be verified as "
    "safe. Refusing rather than allowing an unverifiable write through."
)


def main() -> int:
    """設定ファイルの編集を検知してブロックする。

    Args:
        引数はありません（標準入力から読み取る）。

    Returns:
        終了コード（0: 許可、2: ブロック。ブロック時は emit_block_output が
        host に関わらず常に exit 2 を返しつつ、stdout へ Copilot 等が読む
        permissionDecision: deny JSON も同時に出す合併出力にする）

    Raises:
        例外は発生しません。
    """
    raw, truncated = read_raw_stdin_with_truncation()
    if truncated:
        # 切り捨てられたペイロードで保護判定をすり抜けさせない（fail-closed）。
        return emit_block_output(_truncation_blocked_message(MAX_STDIN_BYTES))

    if not raw:
        # matcher が書込み系ツールに限定しているため、空入力は tty・stdin
        # 未接続など呼び出し自体が想定外の経路。ここは従来通り非ブロッキング。
        return 0

    data = parse_json_object(raw)
    if data is None:
        # matcher により書込み系ツール呼び出しであることは確定している。
        # 非空 stdin が JSON として読めない場合、保護対象ファイルかどうかを
        # 判定できないため fail-closed にする（F-02 対応）。
        return emit_block_output(_UNPARSEABLE_INPUT_MESSAGE)

    reason = _block_reason(data)
    if reason:
        return emit_block_output(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
