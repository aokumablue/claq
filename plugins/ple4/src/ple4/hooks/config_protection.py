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

from ple4.hooks.hook_common import (
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
from ple4.lib.harness import (
    extract_file_paths,
    extract_raw_tool_name,
    is_unidentifiable_tool,
    iter_tool_input_containers,
    normalize_tool_name,
)

# matcher（hooks.json）は Edit/Write/MultiEdit 系のエイリアスにアンカーされた
# 正規表現で、書込み系ツールに限定されている（全ツールではない）。それでも
# 正規化後の値をここで再度絞るのは、matcher の綴りが将来ズレても本体側で
# 書込み系以外を確実に早期 return するための多重防御。
_WRITE_TOOL_NAMES = frozenset({"write", "edit", "multiedit"})

# apply_patch のパッチがパース不能なときの fail-closed 理由。
# 対象ファイルを確定できない入力全般に出す fail-closed の理由。構造化パッチ本文が
# 読めない場合だけでなく、`file_path` がパスとして解釈できない値（数値・入れ子 dict）
# の場合もここに来るため、文言を「パッチ」に限定しない。
_UNDECIDABLE_TARGET_MESSAGE = "BLOCKED: Could not determine target files from tool input."

# frozenset で持つ。判定に使うのは下の `*_FOLDED`（import 時に 1 回だけ畳んだ
# 複製）であり、実行時にこちらへ追加しても保護は増えない。可変のままだと
# その食い違いが黙って通る — このコミットが塞いでいる「片側だけ更新された」
# 型の穴そのものなので、変更を AttributeError で落ちる形にしておく。
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

# ディレクトリ単位で保護する path。basename だけでは判定できない
# （``.git/hooks/pre-commit`` の basename ``pre-commit`` を一律ブロックすると
# 無関係な同名ファイルを巻き込む）ため、実体 path の親を見る。
#
# ``.git/hooks/`` は git 自身のフック本体。``block_no_verify`` は
# ``core.hooksPath`` の差し替えと ``--no-verify`` を塞いでいるが、フック
# スクリプトを直接書き換えれば同じ結果（検査が走らない状態）になる。
# 片側だけ塞ぐと、塞いだ経路の存在が誤った安心になる。
# ``hooks/hooks.json`` はこのプラグイン自身の PreToolUse ガード 4 種の登録元で、
# 1 回の Edit で全部を無効化できる。lint 設定を守って**その lint 検査を登録して
# いるファイル**を放置するのは、`.git/hooks` を保護対象に加えた理由（「片側だけ
# 塞ぐと、塞いだ経路の存在が誤った安心になる」）がそのまま当てはまる。
#
# basename ``hooks.json`` では判定しない。consumer 側の ``.claude/hooks.json``
# は `/harness --apply` が正当に書き込む先で、basename 一致だと巻き込む。
# パス連続一致なら ``plugins/ple4/hooks/hooks.json`` とプラグインキャッシュ配下
# （``~/.claude/plugins/cache/.../hooks/hooks.json``）だけに当たる。
_PROTECTED_PATH_SEGMENTS = ((".git", "hooks"), ("hooks", "hooks.json"))

# 大小無視で比較するための畳み済み複製。`.GIT/HOOKS` は APFS / NTFS では
# `.git/hooks` そのものを指すため、素の比較では保護 path が素通りする（C-2）。
# 表示名には畳む前の綴りを使うので、両者を対で持つ。
_PROTECTED_PATH_SEGMENTS_FOLDED = tuple(
    tuple(normalize_protected_name(part) for part in segments) for segments in _PROTECTED_PATH_SEGMENTS
)

# ファイル名だけでは保護できない汎用設定ファイル。version bump・依存追加
# 等の正当な編集が頻繁なため全面ブロックはしない。書き込み内容が
# lint/format/coverage 設定を弱めうる場合のみ _conditional_block_reason
# でブロックする（R-07）。
CONDITIONALLY_PROTECTED_FILES = frozenset({
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    "package.json",
    # ホスト設定。保護フックを止める手段を 3 つ持つ（`env` の `PLE4_PYTHON`、
    # `hooks` の差し替え、プラグインの無効化）。とくに `PLE4_PYTHON` は
    # `runtime/ple4-hook` が exec するため、保護の無効化ではなく**全ツール
    # 呼び出しでの任意コード実行**になる。
    #
    # 全面保護にはしない。`update-config` skill の中心的な仕事は `env` への
    # `DEBUG=true` や `permissions` の追加であり、`.vscode/settings.json` も
    # 同じ basename で `terminal.integrated.env.*` を持つ。`env` キー全般を
    # 条件にすると正当作業を壊すだけで守れるものが増えない。
    "settings.json",
    "settings.local.json",
})

# 上 2 集合の大小無視の照合用複製。判定は必ずこちらを使い、生の集合は「保護
# 対象は何か」の宣言と表示専用に残す（`bash_config_protection` も畳み済みの
# 側を import して、両フックの判定軸を 1 本に保つ）。
PROTECTED_FILES_FOLDED = frozenset(normalize_protected_name(name) for name in PROTECTED_FILES)
CONDITIONALLY_PROTECTED_FILES_FOLDED = frozenset(
    normalize_protected_name(name) for name in CONDITIONALLY_PROTECTED_FILES
)

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
#
# 行頭の空白は ``[ \t]`` に限る。``\s`` は改行を含むため、``(?m)^`` の各行頭から
# 残りの空白全体を貪欲に取ってから 1 文字ずつ後退する —— 「行頭の数 × 残り空白長」
# で二次オーダーになる（実測: 改行だけの入力で N=5,000 → 0.105s、N=20,000 →
# 1.619s、N=50,000 → 10.187s）。config_protection の hooks.json timeout は 15 秒
# なので約 60,000 バイトの改行だけで超過し、host にフックを殺されれば allow へ
# 倒れる＝保護そのものが無効化される。行頭キーの判定に改行は不要。
_LINT_KEYS = ("ignore", "select", "per-file-ignores", "exclude", "fail_under", "addopts")
_LINT_KEY_PATTERN = re.compile(
    r"(?m)^[ \t]*(" + "|".join(re.escape(key) for key in _LINT_KEYS) + r")[ \t]*="
)

# ホスト設定（settings.json / settings.local.json）で保護を外せるキー。
# `env` 全般ではなく `PLE4_PYTHON` の出現そのものを見る（上の理由）。
# `hooks` は PreToolUse ガードの差し替え、`enabledPlugins` / `disabledPlugins` は
# プラグインごとの無効化に対応する。
_SETTINGS_GUARD_KEYS = ("PLE4_PYTHON", '"hooks"', '"enabledPlugins"', '"disabledPlugins"')

# package.json は TOML/INI ではないため専用のキー照合にする。
_PACKAGE_JSON_LINT_KEYS = ("eslintConfig", "prettier")

# tox.ini の実行コマンドキー。`_LINT_KEYS` へ追加しない理由: `commands` は
# tox.ini 以外（例: pyproject.toml の正当な設定）にも現れうる汎用的な語で
# あり、グローバル追加すると無関係な変更まで deny してしまう。そのため
# tox.ini 限定分岐として独立させる（A-04 対応、package.json の特例と同じ形）。
# 行頭空白を ``[ \t]`` に絞る理由は `_LINT_KEY_PATTERN` と同じ（同型の二次オーダー
# 後退。片側だけ直すと同じ穴が残る）。
_TOX_COMMAND_KEYS = ("commands", "commands_pre", "commands_post")
_TOX_COMMAND_KEY_PATTERN = re.compile(
    r"(?m)^[ \t]*(" + "|".join(re.escape(key) for key in _TOX_COMMAND_KEYS) + r")[ \t]*="
)

# `_text_has_protected_signal` が 1 回の判定で走査するテキストの上限バイト数。
#
# 上のパターンを線形化した後も、判定に投入されるテキスト長そのものは agent 側が
# `MAX_STDIN_BYTES`（1MiB）まで自由に膨らませられる。将来パターンを足したときに
# 同型の後退が再発しても保護が無効化されないよう、走査量に独立した天井を置く。
#
# 超過時は「判定不能」ではなく **True（＝ブロック）** を返す fail-closed とする。
# 打ち切って走査を続ける（truncate）形にすると、上限より後ろに lint キーを置く
# だけで条件付き保護を素通りできる新しいバイパスを自分で作ることになる。
# 条件付き保護対象は pyproject.toml / setup.cfg / tox.ini / package.json の 4 つ
# だけで、そこへ 256KiB を超える単一書き込みを行う正当な操作は無いため、
# fail-closed 側の誤ブロックコストは実質ゼロ。
_LINT_SIGNAL_MAX_TEXT_BYTES = 256 * 1024


def protected_path_segment(file_path: str) -> str | None:
    """パスがディレクトリ単位の保護対象配下か、保護ディレクトリ自身かを判定する。

    symlink は `resolve_effective_target` で解決してから判定する
    （`_effective_basename` と同じ理由。H-02）。解決不能なら生パスで判定する。

    `bash_config_protection` が Bash 経路へ同じ判定を伝播させるため公開名に
    している（`_PROTECTED_PATH_SEGMENTS` 自体は本モジュール private のまま。
    保護対象の定義は本モジュールを単一情報源とし、呼び出し側で
    ``.git`` / ``hooks`` を再定義させない）。

    保護ディレクトリ自身を指すパス（``.git/hooks``）も該当扱いにする。
    ディレクトリごと消す・退避する操作は配下ファイルの書換えと同じ結果に
    なるため、Bash 経路のディレクトリ verb（``rm -rf`` / ``mv``）を取りこぼさない。

    Args:
        file_path: 検査対象の生パス文字列。

    Returns:
        該当した保護 path の表示名（``.git/hooks``）。該当しなければ None。

    Raises:
        例外は発生しません。
    """
    resolved = resolve_effective_target(file_path)
    parts = resolved.parts if resolved is not None else Path(file_path).parts
    # 比較は畳んだ側だけで行い、返す表示名は畳む前の綴りを使う（C-2）。
    folded_parts = tuple(normalize_protected_name(part) for part in parts)
    for segments, folded_segments in zip(
        _PROTECTED_PATH_SEGMENTS, _PROTECTED_PATH_SEGMENTS_FOLDED, strict=True
    ):
        width = len(segments)
        # 保護ディレクトリ自身（``.git/hooks``）も一致させる。当初は親ディレクトリ
        # 列だけを見て末尾一致を除外していたが、それが正しいのは Edit/Write の
        # ようにディレクトリを対象にできない経路だけだった。`bash_config_protection`
        # が本判定を Bash へ伝播させたことで、``rm .git/hooks/pre-commit`` は deny
        # なのに 1 コンポーネント短い ``rm -rf .git/hooks`` は allow という、同じ
        # verb・同じ結果（フックが走らない状態）に対する非対称が生まれた。
        for index in range(len(folded_parts) - width + 1):
            if folded_parts[index : index + width] == folded_segments:
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
        "weakening the config. If this change is genuinely necessary, "
        "ask the user to make it — do not disable the protection hook yourself."
    )


def _paths_from_container(tool_name: str, container: Any) -> list[str] | None:
    """1 つの入力コンテナから対象パスを取り出す。

    構造化パッチが判定不能なら None（呼び出し側は fail-closed）。
    旧 ``file`` キーの補完・list 形状・生文字列形状の展開はいずれも
    ``extract_file_paths`` が担う。ここで dict 限定の補完を持つと、
    list 形状（``[{"file": "ruff.toml"}]``）だけ補完が効かず保護を
    素通りする（実測で exit 0）。共有層に寄せて分岐を二重に持たない。

    Args:
        tool_name: 正規化前の生ツール名。
        container: extract_tool_input 相当の 1 コンテナ値。

    Returns:
        パス一覧。パッチ判定不能時は None。
    """
    return extract_file_paths(tool_name, container)


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
    副判定（`_text_has_protected_signal`）が本命として効く。

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
        # 読み取り量に上限を置く。`pyproject.toml` 等の実ファイルはエージェント
        # 自身が任意サイズへ膨らませられるため、無制限に読むと hooks.json の
        # timeout（5 秒）を host 側から踏まれてフックが殺され allow へ倒れる。
        # lint セクション見出しとキーは先頭付近に現れるので、先頭 MAX_STDIN_BYTES
        # だけで判定に足りる。
        with path.open(encoding="utf-8", errors="replace") as handle:
            text = handle.read(MAX_STDIN_BYTES)
    except OSError:
        return None
    index = text.find(snippet)
    if index < 0:
        return None
    header = _section_header_before(text, index)
    if header is None:
        return False
    return any(header.startswith(candidate) for candidate in _LINT_SECTION_HEADERS)


def _text_has_protected_signal(text: str, file_name: str) -> bool:
    """テキストに保護対象を弱めうる兆候が含まれるか判定する（副判定）。

    ホスト設定（settings.json / settings.local.json）は保護フックを止める
    キー（`PLE4_PYTHON` / `hooks` / `enabledPlugins` / `disabledPlugins`）を、
    それ以外は lint/format/coverage の設定を弱めうる兆候を見る。

    package.json は TOML/INI ではないため専用のキー照合を使う。tox.ini は
    `commands`/`commands_pre`/`commands_post` の実行コマンド変更を専用照合で
    拾う（A-04 対応。見出しを伴わない値行編集で `[testenv]` がスニペットに
    含まれない場合でも検出するため）。それ以外は共通のセクション見出しの
    部分一致、または見出しを伴わない値行編集を拾うためのキー行照合
    （`ignore = [...]` 等）で判定する。

    `_LINT_SIGNAL_MAX_TEXT_BYTES` を超えるテキストは走査せず True（ブロック）を
    返す。走査量の天井は fail-closed 側へ倒す —— 打ち切って走査を続けると、上限
    より後ろに lint キーを置くだけで素通りできるバイパスになる。

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
    # ファイル別分岐も畳んだ名前で選ぶ。basename 側だけ大小無視にすると、
    # ``Package.json`` が条件付き保護には入るのに package.json 専用のキー照合
    # （`eslintConfig`）へ落ちず共通照合で素通りする、という半開きが残る（C-2）。
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
        "protection hooks: PLE4_PYTHON, hooks, enabledPlugins/disabledPlugins). "
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

    if _text_has_protected_signal("\n".join(snippets), file_name):
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
    file_paths = _paths_from_container(tool_name, container)
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
    # できない payload を対象外へ倒すと保護が丸ごと無効になる（S-8）。
    if normalize_tool_name(tool_name).lower() not in _WRITE_TOOL_NAMES and not is_unidentifiable_tool(
        tool_name
    ):
        return None
    # コンテナキーの全走査は iter_tool_input_containers（lib/harness.py）が
    # 単一情報源。フック側で走査を手書きすると片側だけ緩い状態が再発する。
    #
    # 最初の理由で return する（先勝ち）のは意図した短絡。
    # `_block_reason_for_container` の非 None の戻り値は**すべて deny**
    # （判定不能時の `_UNDECIDABLE_TARGET_MESSAGE` を含む）なので、残りの
    # コンテナを評価しても結論は deny のまま変わらない。
    # 「`tool_input` を重くすると `toolArgs` が評価されない」という観測は
    # 短絡ではなく _LINT_KEY_PATTERN の二次オーダー後退が host timeout を
    # 踏んでフックごと殺されていたことの症状であり、そちらを直して塞ぐ。
    for container in iter_tool_input_containers(data):
        reason = _block_reason_for_container(tool_name, container)
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
    try:
        raw, truncated = read_raw_stdin_with_truncation()
    except StdinUnavailableError as exc:
        return emit_block_output(stdin_unreadable_message("pre:config-protection", exc))
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
