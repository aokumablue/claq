"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック。emit_block_output が stderr の理由と
      stdout の permissionDecision: deny JSON を同時に出す)

launcher は stdin を代読しないため、入力切り捨て時のブロックも本モジュール自身が判定する。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, NoReturn

from claq.hooks.block_no_verify import is_sensitive_config_key
from claq.hooks.git_config_text import (
    GLOBAL_GIT_CONFIG_DISPLAY_NAMES,
    GitConfigParseError,
    adds_sensitive_entries,
    git_config_text_after,
    global_git_config_path,
    read_current_text,
)
from claq.hooks.hook_common import (
    basename,
    emit_block_output,
    normalize_protected_name,
    resolve_effective_target,
    resolve_repo_root,
)
from claq.hooks.stdin_io import (
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    parse_json_object,
    read_raw_stdin_with_truncation,
    stdin_unreadable_message,
)
from claq.lib.core_utils import get_plugin_root
from claq.lib.harness import (
    extract_file_paths,
    extract_raw_tool_name,
    is_unidentifiable_tool,
    iter_tool_input_containers,
    normalize_tool_name,
)

# matcher（hooks.json）は書込み系ツールに限定されているが、matcher の綴りがズレても
# 本体側で書込み系以外を早期 return する多重防御。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

# 対象ファイルを確定できない入力全般に出す fail-closed の理由。構造化パッチ本文が
# 読めない場合だけでなく、`file_path` がパスとして解釈できない値の場合も来るため、
# 文言を「パッチ」に限定しない。
_UNDECIDABLE_TARGET_MESSAGE = "BLOCKED: Could not determine target files from tool input."

# frozenset で持つ。判定に使うのは下の `*_FOLDED`（import 時に 1 回だけ畳んだ複製）で、
# 実行時にこちらへ追加しても保護は増えない。その食い違いを黙って通さないよう、
# 変更を AttributeError で落ちる形にしておく。
PROTECTED_FILES = frozenset({
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
})

# ディレクトリ単位で保護する path。basename ``pre-commit`` を一律ブロックすると無関係な
# 同名ファイルを巻き込むため、実体 path の連続した要素で見る。
#
# ``.git/hooks/`` は git 自身のフック本体で、直接書き換えれば ``--no-verify`` と同じ結果
# になる。``hooks/hooks.json`` はこのプラグイン自身の PreToolUse ガードの登録元で、1 回の
# Edit で全部を無効化できる。片側だけ塞ぐと、塞いだ経路の存在が誤った安心になる。
# basename ``hooks.json`` では判定しない（consumer 側の ``.claude/hooks.json`` は
# `/harness --apply` が正当に書き込む先）。
_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"), ("hooks", "hooks.json"))

# 大小無視で比較するための畳み済み複製。`.GIT/HOOKS` は APFS / NTFS では
# `.git/hooks` そのものを指すため、素の比較では保護 path が素通りする。
# 表示名には畳む前の綴りを使うので、両者を対で持つ。
_PROTECTED_PATH_SEGMENTS_FOLDED = tuple(
    tuple(normalize_protected_name(part) for part in segments) for segments in _PROTECTED_PATH_SEGMENTS
)

# 内容次第で保護する git config のパス。remote / branch / user の通常編集が正当にあるため
# 無条件では塞がず、置換後の全文を解析して ``core.hooksPath`` / ``include`` / ``alias`` のように
# フック実行を左右する key（`block_no_verify` が ``git config`` 経由で塞ぐものと同じ集合）が
# 新しく現れる、または値が変わるときだけ塞ぐ。Bash 経由の書込みは内容を解釈できないため
# `bash_config_protection` が書込み先位置に現れた時点で無条件に塞ぐ。
#
# リポジトリ内の設定ファイル（``*`` は worktree 名）。
_GIT_CONFIG_REPO_SEGMENTS = (
    (".git", "config"),
    (".git", "config.worktree"),
    (".git", "worktrees", "*", "config"),
    (".git", "worktrees", "*", "config.worktree"),
)
_GIT_CONFIG_REPO_SEGMENTS_FOLDED = tuple(
    tuple(normalize_protected_name(part) for part in segments) for segments in _GIT_CONFIG_REPO_SEGMENTS
)

# git config として扱う保護 path の表示名の全体（ホーム側を含む）。ブロック文言を選ぶ鍵として公開する。
GIT_CONFIG_DISPLAY_NAMES = frozenset(
    {"/".join(segments) for segments in _GIT_CONFIG_REPO_SEGMENTS} | GLOBAL_GIT_CONFIG_DISPLAY_NAMES
)

# ファイル名だけでは保護できない汎用設定ファイル。version bump・依存追加等の正当な
# 編集が頻繁なため、書き込み内容が保護対象の設定を弱めうる場合のみブロックする。
CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止める手段を持つ（`hooks` の差し替え、プラグインの無効化、
    # インタプリタ解決を乗っ取る `env`）。`model` / `permissions` / `env` の `DEBUG` 等の正当な
    # 編集を壊さないよう、置換前後の全文で `_SETTINGS_GUARDED_*` が変わるときだけ塞ぐ。
    "settings.json",
    "settings.local.json",
})

# 上 2 集合の大小無視の照合用複製。判定は必ずこちらを使う（`bash_config_protection` も
# 畳み済みの側を import して、両フックの判定軸を 1 本に保つ）。
PROTECTED_FILES_FOLDED = frozenset(normalize_protected_name(name) for name in PROTECTED_FILES)
CONDITIONALLY_PROTECTED_FILES_FOLDED = frozenset(
    normalize_protected_name(name) for name in CONDITIONALLY_PROTECTED_FILES
)

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

# 実行中の plugin root から見て守るディレクトリ。`runtime/` は hooks.json が起動する
# `claq-hook`、`src/claq/` はフック本体で、書き換えると次のツール呼び出しから全保護フックが外れる。
_RUNNING_PLUGIN_GUARDED_DIRS = (("runtime",), ("src", "claq"))

# 上の表示名。`blocked_message_for_file` がブロック文言を選ぶ鍵（Edit/Write と Bash で共有）。
RUNNING_PLUGIN_DISPLAY_NAMES = frozenset("/".join(segments) + "/" for segments in _RUNNING_PLUGIN_GUARDED_DIRS)

# package.json は TOML/INI ではないため専用のキー照合にする。
_PACKAGE_JSON_LINT_KEYS = ("eslintConfig", "prettier")

# tox.ini の実行コマンドキー。`commands` は他のファイルにも現れうる汎用的な語なので、
# `_LINT_KEYS` へは足さず tox.ini 限定にする。行頭空白を ``[ \t]`` に絞る理由は
# `_LINT_KEY_PATTERN` と同じ。
_TOX_COMMAND_KEYS = ("commands", "commands_pre", "commands_post")
_TOX_COMMAND_KEY_PATTERN = re.compile(
    r"(?m)^[ \t]*(" + "|".join(re.escape(key) for key in _TOX_COMMAND_KEYS) + r")[ \t]*="
)

# `_text_has_protected_signal` が 1 回の判定で走査するテキストの上限バイト数。将来
# パターンを足して同型の後退が再発しても保護が無効化されないよう、走査量に独立した
# 天井を置く。超過時は True（＝ブロック）を返す。打ち切って走査を続けると、上限より
# 後ろに lint キーを置くだけで素通りできる。条件付き保護対象へ 256KiB を超える単一
# 書き込みを行う正当な操作は無い。
_LINT_SIGNAL_MAX_TEXT_BYTES = 256 * 1024


def _match_path_segments(
    file_path: str,
    candidates: tuple[tuple[str, ...], ...],
    folded_candidates: tuple[tuple[str, ...], ...],
) -> str | None:
    """パスの連続した要素が候補のいずれかに一致するか判定する。

    symlink は解決してから判定し、解決不能なら生パスで判定する。比較は畳んだ側だけで行い、
    返す表示名は畳む前の綴りを使う。候補の要素 ``*`` は任意の 1 要素に一致する。

    Args:
        file_path: 検査対象の生パス文字列。
        candidates: 表示名用の候補（畳む前の綴り）。
        folded_candidates: ``candidates`` を同じ順序で畳んだ複製。

    Returns:
        一致した候補の表示名（``/`` 区切り）。一致しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    folded_parts = tuple(normalize_protected_name(part) for part in parts)
    for segments, folded_segments in zip(candidates, folded_candidates, strict=True):
        width = len(segments)
        for index in range(len(folded_parts) - width + 1):
            window = folded_parts[index : index + width]
            if all(want in ("*", got) for want, got in zip(folded_segments, window, strict=True)):
                return "/".join(segments)
    return None


def protected_path_segment(file_path: str) -> str | None:
    """パスがディレクトリ単位の保護対象配下か、保護ディレクトリ自身かを判定する。

    `bash_config_protection` が Bash 経路へ同じ判定を伝播させるため公開名にしている。
    保護ディレクトリ自身（``.git/hooks``）も該当扱いにする。ディレクトリごと消す・退避する
    操作（``rm -rf .git/hooks``）は配下ファイルの書換えと同じ結果になるため。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/hooks``）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _match_path_segments(file_path, _PROTECTED_PATH_SEGMENTS, _PROTECTED_PATH_SEGMENTS_FOLDED)


def repo_git_config_path(file_path: str) -> str | None:
    """パスがリポジトリ内の git config（``.git/config`` 等）かを判定する。

    Edit/Write は置換後の全文で、Bash は `bash_config_protection` が書込み先位置に現れた時点で
    塞ぐ。後者のため公開名にしている。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/config`` 等）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return _match_path_segments(file_path, _GIT_CONFIG_REPO_SEGMENTS, _GIT_CONFIG_REPO_SEGMENTS_FOLDED)


def git_config_path(file_path: str) -> str | None:
    """パスが内容次第で保護する git config（リポジトリ内・ホーム側）かを判定する。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    return repo_git_config_path(file_path) or global_git_config_path(file_path)


def _effective_basename(file_path: str) -> str:
    """symlink を解決した実体 basename を返す。

    raw path の basename だけを見ると ``alias -> pyproject.toml`` のような symlink 経由の
    書込みがすり抜ける。解決不能（壊れた・循環した symlink 等）な場合は raw path の
    basename にフォールバックする。解決不能を deny に倒すと正当な編集まで巻き込むため。

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

    実行中の plugin root の保護ディレクトリ（`RUNNING_PLUGIN_DISPLAY_NAMES`）には専用の文言を返す。
    Edit/Write と Bash（`bash_config_protection`）が同じ表示名から同じ文言を得るため、選択をここに置く。

    Args:
        file_name: ブロックされたファイル名、または保護 path の表示名。

    Returns:
        ブロックメッセージ文字列

    Raises:
        例外は発生しません。
    """
    if file_name in RUNNING_PLUGIN_DISPLAY_NAMES:
        return _running_plugin_blocked_message(file_name)
    return (
        f"BLOCKED: Modifying {file_name} is not allowed. "
        "Fix the source code to satisfy linter/formatter rules instead of "
        "weakening the config. If this change is genuinely necessary, "
        "ask the user to make it — do not disable the protection hook yourself."
    )


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
            snippet
            for edit in edits
            if isinstance(edit, dict)
            for key in ("old_string", "new_string")
            if isinstance(snippet := edit.get(key), str)
        ]
        return snippets or None

    # edit / search_replace 等はいずれも Edit 相当として扱う
    snippets = [s for s in (container.get("old_string"), container.get("new_string")) if isinstance(s, str)]
    return snippets or None


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
    index = text.find(snippet)
    if index < 0:
        return None
    header: str | None = None
    for line in text[:index].splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            header = stripped
    if header is None:
        return False
    return any(header.startswith(candidate) for candidate in _LINT_SECTION_HEADERS)


def _text_has_protected_signal(text: str, file_name: str) -> bool:
    """テキストに保護対象を弱めうる兆候が含まれるか判定する（副判定）。

    package.json は専用のキーを、tox.ini は実行コマンドのキー（見出しを伴わない値行編集でも
    検出するため）を、それ以外は共通のセクション見出しの部分一致またはキー行照合
    （`ignore = [...]` 等）を見る。
    `_LINT_SIGNAL_MAX_TEXT_BYTES` を超えるテキストは走査せず True を返す。

    Args:
        text: 検査対象テキスト。
        file_name: 判定対象のファイル名（basename）。

    Returns:
        lint/format/coverage 設定を弱めうる兆候があれば True。上限超過も True。

    Raises:
        例外は発生しません。
    """
    if len(text) > _LINT_SIGNAL_MAX_TEXT_BYTES:
        return True
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
        f"{_LINT_SIGNAL_MAX_TEXT_BYTES} bytes, built from a Write or an exact Edit/MultiEdit. "
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
    before = read_current_text(file_path, _LINT_SIGNAL_MAX_TEXT_BYTES + 1)
    if len(before) > _LINT_SIGNAL_MAX_TEXT_BYTES:
        return _settings_unverifiable_message(file_name)
    after = git_config_text_after(tool_name, container, before, _LINT_SIGNAL_MAX_TEXT_BYTES)
    if after is None or len(after) > _LINT_SIGNAL_MAX_TEXT_BYTES:
        return _settings_unverifiable_message(file_name)
    # 深い入れ子は解析・直列化で RecursionError になる。貫通すると exit 1（PreToolUse では allow）。
    try:
        after_obj = _parse_settings_object(after)
        if after_obj is None:
            return _settings_unverifiable_message(file_name)
        before_obj = _parse_settings_object(before) or {}
        # 直列化して比べる。dict の == は true と 1、1 と 1.0 を同じとみなし、型の書換えを見逃す。
        if json.dumps(_settings_guarded_view(before_obj), sort_keys=True) != json.dumps(
            _settings_guarded_view(after_obj), sort_keys=True
        ):
            return _settings_blocked_message(file_name)
    except RecursionError:
        return _settings_unverifiable_message(file_name)
    return None


def _conditional_block_reason(tool_name: str, container: Any, file_path: str, file_name: str) -> str | None:
    """`CONDITIONALLY_PROTECTED_FILES` に該当するファイルへの変更を検査する。

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

    if any(_resolve_lint_section_from_disk(file_path, snippet) is True for snippet in snippets) or (
        _text_has_protected_signal("\n".join(snippets), file_name)
    ):
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
        f"parse as git config text, or the file or the result exceeds {_LINT_SIGNAL_MAX_TEXT_BYTES} bytes. "
        "Refusing an unverifiable write rather than risking a git-hook bypass."
    )


def _git_config_block_reason(tool_name: str, container: Any, file_path: str, display_name: str) -> str | None:
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
    before = read_current_text(file_path, _LINT_SIGNAL_MAX_TEXT_BYTES + 1)
    if len(before) > _LINT_SIGNAL_MAX_TEXT_BYTES:
        return _git_config_unparsable_message(display_name)
    after = git_config_text_after(tool_name, container, before, _LINT_SIGNAL_MAX_TEXT_BYTES)
    if after is None:
        return _unverifiable_conditional_message(display_name)
    if len(after) > _LINT_SIGNAL_MAX_TEXT_BYTES:
        return _git_config_unparsable_message(display_name)
    try:
        if adds_sensitive_entries(before, after, is_sensitive_config_key):
            return _git_config_blocked_message(display_name)
    except GitConfigParseError:
        return _git_config_unparsable_message(display_name)
    return None


def _folded_parts(path: Path) -> tuple[str, ...]:
    """パスの各要素を大小無視で畳む。

    Args:
        path: 畳むパス。

    Returns:
        畳んだ要素列。

    Raises:
        例外は発生しません。
    """
    return tuple(normalize_protected_name(part) for part in path.parts)


def running_plugin_root_is_guarded() -> bool:
    """実行中の plugin root を保護するか（作業中リポジトリ内の開発用 checkout でないか）を判定する。

    claq 自身を開発する checkout では `runtime/` と `src/claq/` の編集が本来の作業なので守らない。
    toplevel が決まらない（リポジトリ外・git 不在・タイムアウト）ときは除外しない。git を起動するので、
    書込み先が保護ディレクトリに当たった後でだけ呼ぶ。

    Returns:
        保護するなら True。

    Raises:
        例外は発生しません。
    """
    repo_root = resolve_repo_root()
    if repo_root is None:
        return True
    plugin_parts = _folded_parts(get_plugin_root())
    repo_parts = _folded_parts(repo_root.resolve())
    return plugin_parts[: len(repo_parts)] != repo_parts


def running_plugin_guarded_dir(resolved: Path, *, include_ancestors: bool) -> str | None:
    """実体パスが実行中の plugin root の保護ディレクトリに当たるかを判定する。

    比較は畳んだ要素列の前方一致で行う（APFS / NTFS では `resolve()` が大小を正規化しない）。

    Args:
        resolved: symlink を解決した書込み先の実体パス。
        include_ancestors: 保護ディレクトリ自身・祖先も当てるか（Bash の削除・退避）。

    Returns:
        当たった保護ディレクトリの表示名（``runtime/`` / ``src/claq/``）。当たらない、または
        開発用 checkout で保護しない場合は None。

    Raises:
        例外は発生しません。
    """
    target = _folded_parts(resolved)
    for segments in _RUNNING_PLUGIN_GUARDED_DIRS:
        guarded = _folded_parts(get_plugin_root().joinpath(*segments))
        if target[: len(guarded)] == guarded or (include_ancestors and guarded[: len(target)] == target):
            return "/".join(segments) + "/" if running_plugin_root_is_guarded() else None
    return None


def _running_plugin_blocked_message(display_name: str) -> str:
    """実行中の plugin root の保護ディレクトリへの書込みに対するブロック理由を生成する。

    Args:
        display_name: 保護ディレクトリの表示名。

    Returns:
        ブロック理由文字列。

    Raises:
        例外は発生しません。
    """
    return (
        f"BLOCKED: Modifying {display_name} of the running claq plugin is not allowed. "
        "Every protection hook runs from this code, so changing it would disable all of them "
        "from the next tool call. If this change is genuinely necessary, ask the user to make it — "
        "do not disable the protection hook yourself."
    )


def _block_reason_for_container(tool_name: str, container: Any) -> str | None:
    """1 つの入力コンテナが保護対象ならブロック理由を返す。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        ブロック理由。保護対象でなければ None。対象ファイルを確定できない
        場合は fail-closed メッセージ。

    Raises:
        例外は発生しません。
    """
    # 旧 ``file`` キーの補完・list 形状・生文字列形状の展開は共有層が担う。ここで dict
    # 限定の補完を持つと、list 形状だけ補完が効かず保護を素通りする。
    file_paths = extract_file_paths(tool_name, container)
    if file_paths is None:
        return _UNDECIDABLE_TARGET_MESSAGE
    for file_path in file_paths:
        # 実体パスを解決できない file_path は位置で照合できないので、下の名前の判定に任せる。
        resolved = resolve_effective_target(file_path)
        if resolved is not None:
            running_dir = running_plugin_guarded_dir(resolved, include_ancestors=False)
            if running_dir is not None:
                return blocked_message_for_file(running_dir)
        protected_segment = protected_path_segment(file_path)
        if protected_segment is not None:
            return blocked_message_for_file(f"{protected_segment}/")
        git_config = git_config_path(file_path)
        if git_config is not None:
            reason = _git_config_block_reason(tool_name, container, file_path, git_config)
            if reason:
                return reason
        file_name = _effective_basename(file_path)
        # 判定は畳んだ名前、メッセージは観測した綴り（利用者が書いた通りの名前が
        # 出ないと、なぜ止まったのかが読み取れなくなる）。
        folded_name = normalize_protected_name(file_name)
        if folded_name in PROTECTED_FILES_FOLDED:
            return blocked_message_for_file(file_name)
        if folded_name in CONDITIONALLY_PROTECTED_FILES_FOLDED:
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
    # 既知の「書き込みではないツール」だけを skip する。ツール名を特定
    # できない payload を対象外へ倒すと保護が丸ごと無効になる。
    if normalize_tool_name(tool_name).lower() not in _WRITE_TOOL_NAMES and not is_unidentifiable_tool(
        tool_name
    ):
        return None
    # 最初の理由で return する（先勝ち）のは意図した短絡。非 None の戻り値はすべて deny
    # なので、残りのコンテナを評価しても結論は変わらない。
    for container in iter_tool_input_containers(data):
        reason = _block_reason_for_container(tool_name, container)
        if reason:
            return reason
    return None


_TRUNCATED_INPUT_MESSAGE = (
    f"BLOCKED: Hook input exceeded {MAX_STDIN_BYTES} bytes for pre:config-protection. "
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

    Returns:
        終了コード（0: 許可、2: ブロック）

    Raises:
        例外は発生しません。
    """
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:config-protection", exc))
    if truncated:
        # 切り捨てられたペイロードで保護判定をすり抜けさせない（fail-closed）。
        return emit_block_output(_TRUNCATED_INPUT_MESSAGE)

    if not raw:
        # matcher が書込み系ツールに限定しているため、空入力は tty・stdin
        # 未接続など呼び出し自体が想定外の経路。ここは非ブロッキング。
        return 0

    data = parse_json_object(raw)
    if data is None:
        # matcher により書込み系ツール呼び出しであることは確定している。
        # 非空 stdin が JSON として読めない場合、保護対象ファイルかどうかを
        # 判定できないため fail-closed にする。
        return emit_block_output(_UNPARSEABLE_INPUT_MESSAGE)

    reason = _block_reason(data)
    if reason:
        return emit_block_output(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
