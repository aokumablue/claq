"""重要な設定ファイルを意図しない変更から保護します。

トリガー: pre:edit, pre:write
入力: 変更されるファイルパスを含むJSON
出力: 保護されたファイルが変更される場合は host 非依存の合併出力でブロック
終了: 0 (許可) または 2 (ブロック。emit_block_output が stderr の理由と
      stdout の permissionDecision: deny JSON を同時に出す)

launcher は stdin を代読しないため、入力切り捨て時のブロックも本モジュール自身が判定する。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from claq.hooks.hook_common import (
    MAX_STDIN_BYTES,
    StdinUnavailableError,
    basename,
    emit_block_output,
    normalize_protected_name,
    parse_json_object,
    read_raw_stdin_with_truncation,
    resolve_effective_target,
    stdin_unreadable_message,
)
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

# ファイル名だけでは保護できない汎用設定ファイル。version bump・依存追加等の正当な
# 編集が頻繁なため、書き込み内容が保護対象の設定を弱めうる場合のみブロックする。
CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止める手段を持つ（`env` の `CLAQ_PYTHON`、`hooks` の差し替え、
    # プラグインの無効化）。`CLAQ_PYTHON` は `runtime/claq-hook` が exec するため、全ツール
    # 呼び出しでの任意コード実行になる。`env` への `DEBUG=true` 等の正当な編集を壊さない
    # よう、全面保護にはしない。
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

# ホスト設定（settings.json / settings.local.json）で保護を外せるキー。`env` 全般ではなく
# `CLAQ_PYTHON` の出現そのものを見る。
_SETTINGS_GUARD_KEYS = ("CLAQ_PYTHON", '"hooks"', '"enabledPlugins"', '"disabledPlugins"')

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


def protected_path_segment(file_path: str) -> str | None:
    """パスがディレクトリ単位の保護対象配下か、保護ディレクトリ自身かを判定する。

    symlink は解決してから判定し、解決不能なら生パスで判定する。`bash_config_protection`
    が Bash 経路へ同じ判定を伝播させるため公開名にしている。保護ディレクトリ自身
    （``.git/hooks``）も該当扱いにする。ディレクトリごと消す・退避する操作
    （``rm -rf .git/hooks``）は配下ファイルの書換えと同じ結果になるため。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/hooks``）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    # 比較は畳んだ側だけで行い、返す表示名は畳む前の綴りを使う。
    folded_parts = tuple(normalize_protected_name(part) for part in parts)
    for segments, folded_segments in zip(
        _PROTECTED_PATH_SEGMENTS, _PROTECTED_PATH_SEGMENTS_FOLDED, strict=True
    ):
        width = len(segments)
        for index in range(len(folded_parts) - width + 1):
            if folded_parts[index : index + width] == folded_segments:
                return "/".join(segments)
    return None


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

    ホスト設定は保護フックを止めるキーを、package.json は専用のキーを、tox.ini は実行
    コマンドのキー（見出しを伴わない値行編集でも検出するため）を、それ以外は共通の
    セクション見出しの部分一致またはキー行照合（`ignore = [...]` 等）を見る。
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
    if folded_name in ("settings.json", "settings.local.json"):
        return any(key in text for key in _SETTINGS_GUARD_KEYS)
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
        "(a lint/format/coverage section or key, or a host setting that can disable the "
        "protection hooks: CLAQ_PYTHON, hooks, enabledPlugins/disabledPlugins). "
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
        "(a file that may contain lint/format/coverage configuration). Refusing an "
        "unverifiable write rather than risking a silent config weakening."
    )


def _conditional_block_reason(tool_name: str, container: Any, file_path: str, file_name: str) -> str | None:
    """`CONDITIONALLY_PROTECTED_FILES` に該当するファイルへの変更を検査する。

    判定は主（ディスク上のセクション解決）・副（テキスト照合）の和集合で行う。内容が
    一切取得できない場合は fail-closed で deny する。

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

    if any(_resolve_lint_section_from_disk(file_path, snippet) is True for snippet in snippets) or (
        _text_has_protected_signal("\n".join(snippets), file_name)
    ):
        return _conditional_blocked_message(file_name)
    return None


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
        protected_segment = protected_path_segment(file_path)
        if protected_segment is not None:
            return blocked_message_for_file(f"{protected_segment}/")
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
