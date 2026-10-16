"""コーディングエージェントハーネスの入力形式差分を吸収する汎用パーサ。

host 判定は一切行わない。フィールド名の union を無条件に受理する寛容パーサ
のみを提供する（tool_input / toolArgs、tool_name / toolName 等の複数命名を
同一の意味へ正規化する）。すべて純 stdlib のみに依存する（venv 不在時の
フォールバック実行を保証するため）。
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Iterator
from typing import Any

# 各ハーネスのツール名 → Claude Code 相当ツール名。
#
# Codex の apply_patch は Edit に対応する。Copilot CLI はフックイベントに
# lowercase の runtime tool 名（write/edit/bash 等）を渡すため、大文字小文字を
# 区別しない照合で Claude Code 表記へ正規化する。
# shell は一部ハーネスが bash 相当として使う runtime 名。
_TOOL_NAME_MAP = {
    "apply_patch": "Edit",
    "agent": "Agent",
    "bash": "Bash",
    "shell": "Bash",
    "edit": "Edit",
    "glob": "Glob",
    "grep": "Grep",
    "multiedit": "MultiEdit",
    "notebookedit": "NotebookEdit",
    "read": "Read",
    "task": "Agent",
    "view": "Read",
    "write": "Write",
    # Grok の runtime tool 名。read_file/list_dir は書き込み系ゲート
    # （config_protection の _WRITE_TOOL_NAMES）の対象外だが、
    # mem/handoff.py の使用ツール記録がハーネス横断で正規化名を
    # 使うために正規化する。
    "search_replace": "Edit",
    "run_terminal_command": "Bash",
    "spawn_subagent": "Agent",
    "read_file": "Read",
    "list_dir": "Glob",
}

# ハーネスごとの tool 入力コンテナキー。存在するキーを順に走査する。
# hooks 側（config_protection / block_no_verify）もこの 1 か所を参照する
# （同じ内容を各所で持つと、キーを足す改修が片方だけに入って取りこぼす）。
INPUT_CONTAINER_KEYS = ("tool_input", "toolInput", "toolArgs", "tool_args")

# 構造化パッチテキストのファイル操作マーカー（Codex apply_patch 形式）
_PATCH_FILE_MARKERS = ("*** Add File: ", "*** Update File: ", "*** Delete File: ")

# ``json.loads`` が「この文字列は JSON として読めない」ことを示すために送出しうる
# 例外。本モジュールと ``hook_common.parse_json_object`` の JSON パースは全て
# この 1 タプルで受ける（片側だけ塞ぐと、同型の穴が別の呼び出し箇所に残る）。
#
# ``RecursionError`` を含めるのは必須。CPython の JSON デコーダは深くネストした
# 配列/オブジェクトに対して ``RecursionError`` を送出する。これを取りこぼすと
# 保護フックが例外で exit 1（= PreToolUse の non-blocking error。ツールはそのまま
# 実行される）へ倒れ、fail-open になる。パース失敗として扱えば生文字列が返り、
# 以降のトークナイザ検査（fail-closed 側）に載る。
JSON_PARSE_FAILURES = (json.JSONDecodeError, TypeError, RecursionError)


def extract_tool_input(payload: dict[str, Any]) -> Any:
    """フック payload から tool_input / toolInput / toolArgs を正規化して返す。

    Claude / VS Code 互換は ``tool_input``、Grok camelCase は ``toolInput``、
    Copilot camelCase は ``toolArgs``（JSON 文字列のことが多い）。文字列で
    JSON オブジェクトに見える場合はパースして返し、パース不能なら元の文字列を返す。
    ``toolInput`` を落とすと Grok の PreToolUse で tool 入力が取れず、
    ``block_no_verify`` が全 ``run_terminal_command`` を exit 2 で拒否する。

    Returns:
        正規化後の tool 入力（dict / str / その他）、キーが無ければ None。
    """
    for key in INPUT_CONTAINER_KEYS:
        if key not in payload:
            continue
        value = payload[key]
        if not isinstance(value, str):
            return value
        # 判定とパースは同じ文字列に対して行う。lstrip 後の値で「JSON らしさ」を
        # 見ながら元の文字列をパースすると、JSON が許容しない空白（\x0c 等）を
        # 1 文字前置しただけで「{ で始まるがパースできない」状態になり、生コマンド
        # 文字列として扱われる。その文字列の中では危険なコマンドが JSON のダブル
        # クォート内に入るため、トークナイザからは実行トークンに見えず素通りする。
        stripped = value.lstrip()
        if not stripped.startswith(("{", "[")):
            return value
        try:
            return json.loads(stripped)
        except JSON_PARSE_FAILURES:
            return value
    return None


def extract_raw_tool_name(payload: dict[str, Any]) -> str:
    """フック payload から正規化前の生ツール名を返す。

    非空の ``tool_name`` を優先し、無ければ非空の ``toolName`` を使う。
    空文字・非文字列は無効として次候補へ倒す。どちらも無効なら空文字。
    """
    tool_name = payload.get("tool_name")
    if isinstance(tool_name, str) and tool_name:
        return tool_name
    camel_name = payload.get("toolName")
    if isinstance(camel_name, str) and camel_name:
        return camel_name
    return ""


def iter_tool_input_containers(payload: dict[str, Any]) -> Iterator[Any]:
    """payload に存在する全コンテナキーの正規化済み tool 入力を順に返す。

    ``extract_tool_input`` は先勝ちで 1 キーだけを返す。それを単独で使うと、
    複数のコンテナキーを同時に載せた payload で先頭の無害な側だけを検査し、
    後続キーに入った危険な入力を素通りさせる。全キー走査は本関数を単一情報源とする。
    """
    for key in INPUT_CONTAINER_KEYS:
        if key in payload:
            yield extract_tool_input({key: payload[key]})


def iter_bash_commands(payload: dict[str, Any]) -> Iterator[str]:
    """フック payload に含まれる Bash/shell の command 文字列を全て返す。

    ``tool_input.command`` / ``toolArgs``（JSON 文字列含む）/ 生文字列 / list 形状を
    吸収し、コンテナキーは 1 つも取りこぼさず走査する。list 形状を取りこぼすと、
    コマンドが 1 つも無い payload として静かに許可されてしまう。

    1 つも yield しない場合の扱いは呼び出し側の責務。コンテナキー自体が無い場合の
    fail-closed は ``block_no_verify`` が ``extract_tool_input`` の None 判定で別途行う。

    Yields:
        非空の command 文字列。
    """
    for container in iter_tool_input_containers(payload):
        yield from _commands_from_tool_input(container)


def _commands_from_tool_input(tool_input: Any) -> list[str]:
    """正規化済み tool 入力から command 文字列を**すべて**取り出す。

    フィールド別名（``command`` / ``cmd``）は 1 つも取りこぼさず走査する。先勝ちに
    すると、無害な ``command`` を 1 つ足すだけで ``cmd`` の危険なコマンドが検査から
    外れる（dict の挿入順ではなくキーの順序が結果を決めるため、並びを問わない）。

    Returns:
        非空のコマンド文字列のリスト。list 形状は要素ごとに再帰して連結する。要素が 2 つ
        以上で全て str の list（argv 配列）は、要素ごとの走査に加えて ``shlex.join`` で
        1 コマンドへ結合した文字列も末尾に返す。要素単位でしか見ないと
        ``["git", "commit", "--no-verify"]`` の各要素は単独では無害に読め、全フックを
        素通りする。
    """
    if isinstance(tool_input, dict):
        # 値は文字列とは限らない。``{"command": ["bash", "-lc", "git commit
        # --no-verify -m x"]}`` のように argv 配列で渡す host があり、文字列
        # 以外を捨てるとコマンドが 1 つも無い payload として静かに許可される。
        return [
            command
            for key in ("command", "cmd")
            if key in tool_input
            for command in _commands_from_tool_input(tool_input[key])
        ]
    if isinstance(tool_input, str):
        return [tool_input] if tool_input else []
    if isinstance(tool_input, list):
        commands = [command for item in tool_input for command in _commands_from_tool_input(item)]
        if len(tool_input) > 1 and all(isinstance(item, str) for item in tool_input):
            commands.append(shlex.join(tool_input))
        return commands
    return []


def normalize_tool_name(tool_name: str) -> str:
    """ハーネス固有のツール名を Claude Code 相当のツール名へ正規化する。

    大文字小文字を区別せず ``_TOOL_NAME_MAP`` で引き、対象外はそのまま返す。
    Claude Code に apply_patch というツールは存在せず、Claude Code 自身のツール名は
    既に正規形のため、ハーネス判定なしの無条件マッピングで安全。
    """
    return _TOOL_NAME_MAP.get(tool_name.lower(), tool_name)


# 本パーサが「どのツールか特定できた」と言える正規化後の名前（小文字比較）。
# `_TOOL_NAME_MAP` の値から導出する。写経すると片方だけ更新されて判定がずれる。
_IDENTIFIABLE_TOOL_NAMES = frozenset(name.lower() for name in _TOOL_NAME_MAP.values())


def is_unidentifiable_tool(tool_name: str) -> bool:
    """ツール名が既知のどれとも一致しないかを判定する。

    ツール名で検査対象を絞るフックは、名前を特定できない payload（空文字へ倒れた
    ``{"tool_name": 123}`` や未知の ``mcp__fs__write``）を「対象外」と読み替えては
    ならない。ツール名の照合はハーネスの matcher が既に済ませているため、「既知の
    対象外ツール」だけを skip し、特定できない名前は検査側へ倒す
    （docs/adr/shell-analysis-boundary.md: 誤検出 > 誤通過）。
    """
    return normalize_tool_name(tool_name).lower() not in _IDENTIFIABLE_TOOL_NAMES


def _extract_patch_text(tool_input: dict | str | None) -> str | None:
    """入力から構造化パッチ本文候補を取り出す。

    Copilot CLI では生のパッチ文字列、他ハーネスでは {"input": "..."} のような dict、
    あるいはその JSON 文字列で渡るため、すべて吸収する。判定は呼び出し側が行う。

    判定とパースは同じ ``lstrip`` 後の文字列に対して行う。``\\x0c`` / ``\\x0b`` は
    Python の空白だが JSON の空白ではないため、元の文字列をパースすると 1 文字前置
    するだけでパースが失敗し、patch 本文ではなく生の JSON 文字列がパス候補として返る。
    """
    if isinstance(tool_input, dict):
        patch_text = tool_input.get("input")
        return patch_text if isinstance(patch_text, str) else None

    if not isinstance(tool_input, str):
        return None

    stripped = tool_input.lstrip()
    if not stripped.startswith("{"):
        return tool_input
    try:
        parsed = json.loads(stripped)
    except JSON_PARSE_FAILURES:
        return tool_input
    patch_text = parsed.get("input")
    return patch_text if isinstance(patch_text, str) else tool_input


def _has_patch_markers(patch_text: str) -> bool:
    """テキストが構造化パッチのファイル操作マーカー行を 1 つ以上含むか判定する。"""
    return any(line.startswith(marker) for line in patch_text.splitlines() for marker in _PATCH_FILE_MARKERS)


def _concat_or_none(parts: Iterable[list[str] | None]) -> list[str] | None:
    """パス一覧を連結する。1 つでも判定不能（None）があれば None を返す。"""
    paths: list[str] = []
    for nested in parts:
        if nested is None:
            return None
        paths.extend(nested)
    return paths


def _path_strings(value: Any) -> list[str] | None:
    """`file_path` / `file` フィールドの値をパス文字列のリストへ平坦化する。

    値は文字列とは限らない。``{"file_path": ["ruff.toml"]}`` のように配列で渡す host が
    あり、文字列以外を捨てると「対象ファイルが 1 つも無い書き込み」として静かに許可される。

    Returns:
        パス文字列のリスト（空文字は除く）。文字列でも list でもない値はパスとして
        解釈できないため None を返し、呼び出し側で fail-closed に倒す
        （docs/adr/shell-analysis-boundary.md: 誤検出 > 誤通過）。
    """
    if isinstance(value, str):
        return [value] if value else []
    if not isinstance(value, list):
        return None
    return _concat_or_none(_path_strings(item) for item in value)


def extract_file_paths(tool_name: str, tool_input: dict | str | None) -> list[str] | None:
    """ツール入力から操作対象のファイルパス一覧を抽出する。

    Edit/Write/MultiEdit は file_path フィールド（旧名 ``file`` も同義）を使う。
    構造化パッチ（Copilot/Codex の apply_patch 等）はマーカー行をパースする。パッチか
    どうかは tool_name だけに頼らず、マーカー行の有無でも判定する（ハーネスごとの
    命名差異でツール名が "apply_patch" と一致しなくても検査から漏らさないため）。

    list 形状（``[{"file_path": "..."}]`` や ``["..."]``）と生文字列形状
    （``"ruff.toml"``）も展開する。ここで空リストを返すと、``config_protection`` は
    「対象ファイルが 1 つも無い書き込み」として静かに許可してしまう。

    Args:
        tool_name: フック stdin の tool_name フィールド値（正規化前）。
        tool_input: フック stdin の tool_input フィールド値。dict / list / 文字列 / None。

    Returns:
        ファイルパスのリスト。判定不能（パッチ本文と分かっているのにマーカーが 1 つも
        見つからない等）の場合は None。呼び出し側は None を fail-closed として扱うこと。
    """
    patch_text = _extract_patch_text(tool_input)
    is_declared_patch_tool = tool_name == "apply_patch"
    if isinstance(patch_text, str) and (is_declared_patch_tool or _has_patch_markers(patch_text)):
        paths = [
            line[len(marker) :].strip()
            for line in patch_text.splitlines()
            for marker in _PATCH_FILE_MARKERS
            if line.startswith(marker)
        ]
        return paths or None

    if isinstance(tool_input, list):
        paths = _concat_or_none(extract_file_paths(tool_name, item) for item in tool_input)
        # apply_patch と明示されているのに対象を 1 つも取り出せない: 判定不能
        return None if is_declared_patch_tool and not paths else paths

    if is_declared_patch_tool:
        # apply_patch と明示されているのにパッチ本文を取り出せない: 判定不能
        return None

    if isinstance(tool_input, str):
        return [tool_input] if tool_input else []

    if not isinstance(tool_input, dict):
        return []

    # フィールド別名は 1 つも取りこぼさず走査する。先勝ちにすると、無害な `file_path`
    # を 1 つ足すだけで `file` の保護対象パスが検査から外れる。判定不能（None）は
    # そのまま伝播させ fail-closed。
    return _concat_or_none(_path_strings(tool_input[key]) for key in ("file_path", "file") if key in tool_input)


# ユーザー発話として transcript に載るが、実際にはハーネスが生成した足場で
# あって依頼ではないタグ。中身ごと捨てる。
#
# - local-command-caveat: ローカルコマンド実行時の定型注意書き。「DO NOT
#   respond to these messages」という指示文を含むため、依頼として引き継ぐと
#   次セッションへ疑似ユーザー指示として再注入されてしまう。
# - local-command-stdout / local-command-stderr: コマンドの出力であって依頼ではない。
# - task-notification: サブエージェント完了通知。ユーザーの発話ではない。
# - system-reminder: ハーネスが user ターンへ差し込む指示文・ファイル内容。
# - agent-message: 別セッション / 別エージェントからの SendMessage 配信足場。
#   中身は他エージェントの命令文であり、依頼として引き継ぐと次セッションへ
#   疑似ユーザー指示として再注入される。
_SCAFFOLD_TAGS = (
    "local-command-caveat",
    "local-command-stdout",
    "local-command-stderr",
    "task-notification",
    "system-reminder",
    "agent-message",
)

# スラッシュコマンド起動の足場タグ。中身は依頼そのものなので捨てずに畳む。
_COMMAND_TAGS = ("command-name", "command-message", "command-args")

# 診断側（ci/scan_scaffold_drift.py）が「既知タグ」を組み立てるための公開名。
# 値は上の定義から導出する。写経すると片方だけ更新されて誤報が出る。
SCAFFOLD_TAGS = _SCAFFOLD_TAGS
COMMAND_TAGS = _COMMAND_TAGS

# タグ名の直後に「名前の終わり」を要求する先読み。これが無いと、タグ**名**が
# 足場名で始まるだけの別タグ（`<system-reminders>` や `<local-command-stdout-parser>`）
# まで一致し、無関係な依頼をメッセージごと破棄してしまう。属性付きは通す。
_TAG_NAME_END = r"(?=[\s/>])"

# 孤立タグ検出専用の「名前の終わり」。除去側と違い**文字列の終端も名前の終わりと
# みなす**。除去側の先読みは名前の後ろに 1 文字を要求するため、発話が足場タグ名で
# 終わる入力（`"作業完了 <system-reminder"`）はブロック除去にも孤立検出にも一致せず、
# docs/adr/untrusted-input-prompt-boundary.md の fail closed が発火しない。
#
# 除去側を広げてはならない。`$` を許すと閉じない開始タグが「名前の終わり」を満たし、
# 消す側の精度要求（`<system-reminders>` を巻き込まない）と噛み合わない。
_ORPHAN_NAME_END = r"(?=[\s/>]|$)"

# タグの属性部に許す最大文字数。無界にすると `>` を含まない入力
# （`"<system-reminder " * N`）で二次オーダーになる（SessionEnd で毎回通る経路）。
#
# **この上限は複雑度のための装置であって、安全性の境界ではない。** 上限を使うのは
# 除去パターンだけで、破棄判定の `_SCAFFOLD_ORPHAN_PATTERN` は使わない。超過して除去
# できなかったブロックは孤立タグとして検出され、メッセージごと破棄される。
_MAX_TAG_ATTR_CHARS = 512
_TAG_ATTRS = rf"[^>]{{0,{_MAX_TAG_ATTR_CHARS}}}"


def _tag_block_pattern(tag: str, *, capture: bool = False) -> re.Pattern[str]:
    """``<tag ...>中身</tag>`` のブロックに一致するパターンを返す。

    中身は「同種の開始タグを含まない任意の文字列」に限る。単純な `.*?` だと、対を
    成さない開始タグから後続の別ブロックの閉じタグまで貫通して間の実依頼まで巻き込む
    うえ、閉じタグ無しの開始タグが大量にあると走査コストが桁違いに膨らむ。

    Args:
        tag: タグ名。
        capture: True なら前後の空白を除いた中身をグループ 1 として捕捉する。
    """
    body = rf"(?:(?!<{tag}{_TAG_NAME_END})[\s\S])*?"
    if capture:
        body = rf"\s*({body})\s*"
    return re.compile(rf"<{tag}{_TAG_NAME_END}{_TAG_ATTRS}>{body}</{tag}\s*>", re.IGNORECASE)


# タグごとに自分自身の開始タグを否定先読みする必要があるため、1 本の alternation に
# まとめられずタグ単位のリストになる。
_SCAFFOLD_BLOCK_PATTERNS = [_tag_block_pattern(tag) for tag in _SCAFFOLD_TAGS]

# ペア除去後に残った足場タグ。ハーネスは足場タグを必ず対で書き、`_read_tail` の
# 前方切り詰めで壊れる先頭 1 行は `_parse_entry` が不正 JSON として捨てるため、
# この段階で片側だけ残っているタグは、足場の中身に閉じタグリテラルを混ぜてブロックを
# 早期終端させた細工とみなす。断片を救う利得より、細工した文字列が「直近の依頼」として
# 次セッションへ注入される損失のほうが大きいため、メッセージごと破棄する。
#
# 属性部（`_TAG_ATTRS`）と閉じ括弧は**要求しない**。要求すると、属性が 512 文字を
# 超えるタグがブロック除去にも本検出にも一致せず素通りする。先読みも選択肢も固定幅
# なので走査は入力長に対して線形。
_SCAFFOLD_ORPHAN_PATTERN = re.compile(
    r"</?(?:" + "|".join(_SCAFFOLD_TAGS) + r")" + _ORPHAN_NAME_END, re.IGNORECASE
)

# スラッシュコマンド起動の足場。`<command-name>` と `<command-args>` の中身は
# ユーザーが実際に入力した依頼そのものなので、捨てずに `/name args` へ畳む。
_COMMAND_NAME_PATTERN = _tag_block_pattern("command-name", capture=True)
_COMMAND_ARGS_PATTERN = _tag_block_pattern("command-args", capture=True)
_COMMAND_SCAFFOLD_PATTERN = re.compile(
    r"</?(?:" + "|".join(_COMMAND_TAGS) + r")" + _TAG_NAME_END + _TAG_ATTRS + r">", re.IGNORECASE
)
# 畳み込みで中身ごと落とすブロック。`command-message` はコマンド名の再掲、
# `command-args` は畳んだ文字列側へ取り込み済みのため元の位置には残さない。
_COMMAND_MESSAGE_BLOCK_PATTERN = _tag_block_pattern("command-message")
_COMMAND_ARGS_BLOCK_PATTERN = _tag_block_pattern("command-args")


def _drop_scaffold_blocks(text: str) -> str | None:
    """依頼ではないハーネス足場タグを中身ごと除去する。

    ペア除去後に片側だけのタグが残った場合は、細工された入力とみなして
    メッセージ自体を破棄する（fail closed）。

    「足場タグの総数が上限を超えたら破棄する」ガードは持たない。走査は否定先読みで
    線形に収まり、除去回避は孤立タグ検出が 1 個でも破棄するため上限に依存しない。
    上限を置くと、対になった足場ブロックを大量に含む正当な依頼を落とす誤検知だけが残る。

    Returns:
        足場タグとその中身を除いたテキスト。メッセージごと破棄すべきなら None。
    """
    for pattern in _SCAFFOLD_BLOCK_PATTERNS:
        text = pattern.sub("", text)
    if _SCAFFOLD_ORPHAN_PATTERN.search(text):
        return None
    return text


def _fold_command_invocation(text: str) -> str:
    """スラッシュコマンド起動の足場を ``/name args`` へ畳んで元の位置へ差し込む。

    メッセージ全体を置き換えてはならない。コマンド起動と地の文が同居する場合に地の文が
    消えるうえ、注入済みの ``<claq-memory>`` ブロック内に残った前回の ``<command-name>``
    のエコーが実依頼を押し退ける（後段の ``strip_tags`` が記憶ブロックを中身ごと
    落とせるよう、畳んだ結果もブロックの内側に留める）。

    畳み込みは**起動単位**で行う。最初の ``<command-name>`` と最初の ``<command-args>``
    を無条件にペアリングすると、後続の起動の引数が前の起動へ付け替わり、その引数を
    含む実依頼が記憶ブロックごと消える。各 ``<command-name>`` から次の
    ``<command-name>`` の直前までを 1 起動の範囲とし、その範囲内でだけ引数を探す。

    コマンド名が取れない場合でも、残った ``command-*`` タグ自体は依頼本文ではない
    ため無条件に落とす。
    """
    matches = list(_COMMAND_NAME_PATTERN.finditer(text))
    if not matches:
        return _COMMAND_SCAFFOLD_PATTERN.sub("", text)

    parts: list[str] = [text[: matches[0].start()]]
    for index, match in enumerate(matches):
        region_end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        region = text[match.end() : region_end]
        name = match.group(1).strip().lstrip("/")
        if name:
            args_match = _COMMAND_ARGS_PATTERN.search(region)
            args = args_match.group(1).strip() if args_match else ""
            parts.append(f"/{name} {args}".strip())
            region = _COMMAND_ARGS_BLOCK_PATTERN.sub("", region, count=1)
            region = _COMMAND_MESSAGE_BLOCK_PATTERN.sub("", region)
        parts.append(region)
    return _COMMAND_SCAFFOLD_PATTERN.sub("", "".join(parts))


def normalize_user_message(text: str) -> str:
    """ユーザー発話からハーネス生成の足場を落として依頼本文だけを残す。

    transcript の ``user`` エントリには、ハーネスが生成した足場（ローカルコマンドの
    注意書き・その stdout・サブエージェント完了通知・system-reminder・スラッシュ
    コマンドの起動タグ）も同じ形で載る。これらを依頼として引き継ぐと、実際の依頼が
    押し出されるうえ、注意書きの指示文が次セッションへ疑似ユーザー指示として再注入される。

    中身が無価値な足場は丸ごと捨て、スラッシュコマンド起動だけは ``/name args`` へ
    畳む。足場の中身に閉じタグを混ぜて除去を回避しようとした入力はメッセージごと破棄する。

    Returns:
        依頼本文。足場しか含まれていない、または細工が検出された場合は空文字列。
    """
    if not text:
        return text
    stripped = _drop_scaffold_blocks(text)
    if stripped is None:
        return ""
    return _fold_command_invocation(stripped).strip()
